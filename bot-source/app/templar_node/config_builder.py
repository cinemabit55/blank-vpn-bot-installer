"""Generate node config YAML files from CLI answers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.templar_node.schemas import DEFAULT_XHTTP_SERVER_EXTRA, NodeConfig, NodeRole, RealityStrategy, TransitMode


DEFAULT_PUBLIC_PORT = 443
DEFAULT_NODE_PORT = 2222
DEFAULT_XTLS_API_PORT = 61000
DEFAULT_REMOTE_DEST_TARGET = 'ya.ru:443'
DEFAULT_REMOTE_DEST_SERVER_NAME = 'ya.ru'


class ConfigBuildError(ValueError):
    """Raised when config generation input is incomplete or inconsistent."""


@dataclass(frozen=True)
class TariffTargets:
    slugs: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    trial_eligible: bool = False

    def has_any(self) -> bool:
        return bool(self.slugs or self.names or self.trial_eligible)

    def require_any(self, label: str) -> None:
        if not self.has_any():
            raise ConfigBuildError(f'{label} must include at least one tariff slug/name or trial eligibility')


@dataclass(frozen=True)
class CommonGenerationInput:
    main_ipv4: str
    remnawave_api_url: str
    admin_allowlist: tuple[str, ...]
    admin_user: str = 'templar'
    ssh_port: int = 22
    dns_api_token_ref: str = 'secrets/dns-api-token'


@dataclass(frozen=True)
class NodeGenerationInput:
    internal_name: str
    display_name: str
    country_code: str
    domain: str | None
    public_ipv4: str
    public_ipv6: str | None = None
    spare_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class CascadeDirectInput:
    common: CommonGenerationInput
    foreign: NodeGenerationInput
    ru_edge: NodeGenerationInput
    foreign_tariffs: TariffTargets
    ru_edge_tariffs: TariffTargets
    service_user: str = 'bridge_ru_to_foreign'
    foreign_reality_strategy: str = 'local_decoy_site'
    foreign_remote_dest_target: str = DEFAULT_REMOTE_DEST_TARGET
    foreign_remote_dest_server_names: tuple[str, ...] = ()
    ru_edge_reality_strategy: str = 'local_decoy_site'
    ru_edge_remote_dest_target: str = DEFAULT_REMOTE_DEST_TARGET
    ru_edge_remote_dest_server_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuWarpInput:
    common: CommonGenerationInput
    node: NodeGenerationInput
    tariffs: TariffTargets
    reality_strategy: str = 'local_decoy_site'
    remote_dest_target: str = DEFAULT_REMOTE_DEST_TARGET
    remote_dest_server_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtraRuEdgeInput:
    foreign_config: NodeConfig
    ru_edge: NodeGenerationInput
    tariffs: TariffTargets
    reality_strategy: str = 'local_decoy_site'
    remote_dest_target: str = DEFAULT_REMOTE_DEST_TARGET
    remote_dest_server_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedConfigSet:
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate(self) -> None:
        for filename, raw in self.configs.items():
            try:
                NodeConfig.model_validate(raw)
            except ValidationError as exc:
                raise ConfigBuildError(f'generated config {filename} is invalid: {exc}') from exc


def generate_cascade_direct(inputs: CascadeDirectInput) -> GeneratedConfigSet:
    inputs.foreign_tariffs.require_any('foreign direct tariffs')
    inputs.ru_edge_tariffs.require_any('RU cascade tariffs')
    if inputs.foreign_reality_strategy not in ('local_decoy_site', 'remote_dest'):
        raise ConfigBuildError('foreign exit reality_strategy must be local_decoy_site or remote_dest')
    if inputs.ru_edge_reality_strategy not in ('local_decoy_site', 'remote_dest'):
        raise ConfigBuildError('RU edge reality_strategy must be local_decoy_site or remote_dest')
    if inputs.foreign_reality_strategy == 'local_decoy_site':
        _required_domain(inputs.foreign, 'foreign exit')
    if inputs.ru_edge_reality_strategy == 'local_decoy_site':
        _required_domain(inputs.ru_edge, 'RU edge')
    foreign = _foreign_exit_config(inputs)
    ru_edge = _ru_edge_config(inputs)
    generated = GeneratedConfigSet(
        {
            f'{_file_stem(inputs.foreign.internal_name)}.yml': foreign,
            f'{_file_stem(inputs.ru_edge.internal_name)}.yml': ru_edge,
        },
    )
    generated.validate()
    return generated


def generate_ru_warp(inputs: RuWarpInput) -> GeneratedConfigSet:
    inputs.tariffs.require_any('RU WARP tariffs')
    if inputs.reality_strategy not in ('local_decoy_site', 'remote_dest'):
        raise ConfigBuildError('RU WARP reality_strategy must be local_decoy_site or remote_dest')
    if inputs.reality_strategy == 'local_decoy_site' and not inputs.node.domain:
        raise ConfigBuildError('RU WARP local_decoy_site strategy requires a node domain')
    config = _base_config(inputs.common, inputs.node, inputs.tariffs)
    config['role'] = 'ru-warp'
    config['warp'] = _warp_config(inputs.node.internal_name)
    config['transit'] = {'mode': 'disabled'}
    if inputs.reality_strategy == 'remote_dest':
        _apply_remote_dest_ru_warp_config(config, inputs)
    generated = GeneratedConfigSet({f'{_file_stem(inputs.node.internal_name)}.yml': config})
    generated.validate()
    return generated


def generate_extra_ru_edge(inputs: ExtraRuEdgeInput) -> GeneratedConfigSet:
    inputs.tariffs.require_any('RU cascade tariffs')
    _validate_foreign_exit_for_extra_ru_edge(inputs.foreign_config)
    if inputs.reality_strategy not in ('local_decoy_site', 'remote_dest'):
        raise ConfigBuildError('RU edge reality_strategy must be local_decoy_site or remote_dest')
    if inputs.reality_strategy == 'local_decoy_site' and not inputs.ru_edge.domain:
        raise ConfigBuildError('RU edge local_decoy_site strategy requires a node domain')
    config = _ru_edge_config_from_foreign(inputs)
    generated = GeneratedConfigSet({f'{_file_stem(inputs.ru_edge.internal_name)}.yml': config})
    generated.validate()
    return generated


def build_foreign_config_with_extra_ru_edge(
    foreign_config: NodeConfig,
    ru_edge_ipv4: str,
    ru_edge_ipv6: str | None = None,
) -> dict[str, Any]:
    _validate_foreign_exit_for_extra_ru_edge(foreign_config)
    raw = foreign_config.model_dump(mode='json')
    allow_from = list(raw['transit'].get('allow_from') or [])
    for source in _transit_allow_sources(ru_edge_ipv4, ru_edge_ipv6):
        if source not in allow_from:
            allow_from.append(source)
    raw['transit']['allow_from'] = allow_from
    try:
        NodeConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigBuildError(f'updated foreign config is invalid: {exc}') from exc
    return raw


def write_yaml_config(raw: dict[str, Any], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return output


def write_generated_configs(generated: GeneratedConfigSet, output_dir: str | Path) -> list[Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, raw in generated.configs.items():
        path = root / filename
        data = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
        path.write_text(data, encoding='utf-8')
        written.append(path)
    return written


def _foreign_exit_config(inputs: CascadeDirectInput) -> dict[str, Any]:
    config = _base_config(inputs.common, inputs.foreign, inputs.foreign_tariffs)
    config['role'] = 'foreign-exit'
    config['warp'] = _warp_config(inputs.foreign.internal_name)
    if inputs.foreign_reality_strategy == 'remote_dest':
        _apply_remote_dest_foreign_config(config, inputs)
    config['transit'] = {
        'mode': 'vless_reality',
        'inbound_tag': _node_inbound_tag('VLESS_Transit', inputs.foreign.internal_name),
        'listen_port': 10443,
        'flow': 'xtls-rprx-vision',
        'allow_from': _transit_allow_sources(inputs.ru_edge.public_ipv4, inputs.ru_edge.public_ipv6),
        'service_user': inputs.service_user,
        'service_user_credential_ref': _secret_ref('transit-user', inputs.foreign.internal_name),
        'reality_credentials_ref': _secret_ref('transit-reality', inputs.foreign.internal_name),
    }
    config['host']['port'] = config['transit']['listen_port']
    config['host']['inbound_ref'] = 'transit'
    if inputs.foreign_reality_strategy == 'remote_dest':
        config['host']['address'] = inputs.foreign.public_ipv4
    else:
        config['host']['address'] = (
            _ipv6_host_alias(_required_domain(inputs.foreign, 'foreign exit'))
            if inputs.foreign.public_ipv6
            else inputs.foreign.public_ipv4
        )
    return config


def _transit_allow_sources(public_ipv4: str, public_ipv6: str | None = None) -> list[str]:
    sources = [public_ipv4]
    if public_ipv6:
        sources.append(public_ipv6)
    return sources


def _foreign_transit_endpoint(*, domain: str, ipv6: str | None, ru_edge_ipv6: str | None) -> str:
    if ipv6 and ru_edge_ipv6:
        return ipv6
    return domain


def _foreign_transit_endpoint_from_inputs(inputs: CascadeDirectInput) -> str:
    if inputs.foreign.public_ipv6 and inputs.ru_edge.public_ipv6:
        return inputs.foreign.public_ipv6
    if inputs.foreign_reality_strategy == 'remote_dest':
        return inputs.foreign.public_ipv4
    return _required_domain(inputs.foreign, 'foreign exit')


def _foreign_server_names_from_inputs(inputs: CascadeDirectInput) -> list[str]:
    if inputs.foreign_reality_strategy == 'remote_dest':
        return list(inputs.foreign_remote_dest_server_names or (_remote_dest_host(inputs.foreign_remote_dest_target),))
    return [_required_domain(inputs.foreign, 'foreign exit')]


def _foreign_transit_endpoint_from_config(foreign: NodeConfig, *, ru_edge_ipv6: str | None) -> str:
    if foreign.public_ipv6 and ru_edge_ipv6:
        return foreign.public_ipv6
    if foreign.reality.strategy == RealityStrategy.REMOTE_DEST:
        return foreign.public_ipv4
    return foreign.domain


def _ipv6_host_alias(domain: str) -> str:
    first_label, separator, suffix = domain.partition('.')
    if not separator:
        return f'{domain}-v6'
    return f'{first_label}-v6.{suffix}'


def _node_inbound_tag(prefix: str, internal_name: str) -> str:
    suffix = ''.join(ch.upper() if ch.isalnum() else '_' for ch in internal_name).strip('_')
    while '__' in suffix:
        suffix = suffix.replace('__', '_')
    return f'{prefix}_{suffix or "NODE"}'


def _ru_edge_config(inputs: CascadeDirectInput) -> dict[str, Any]:
    config = _base_config(inputs.common, inputs.ru_edge, inputs.ru_edge_tariffs)
    config['role'] = 'ru-edge'
    config['warp'] = {'mode': 'disabled'}
    if inputs.ru_edge_reality_strategy == 'remote_dest':
        _apply_remote_dest_ru_edge_config(config, inputs)
    config['transit'] = {
        'mode': 'vless_reality',
        'outbound_tag': 'TRANSIT_TO_FOREIGN',
        'foreign_exit_domain': _foreign_transit_endpoint_from_inputs(inputs),
        'foreign_exit_port': 10443,
        'server_names': _foreign_server_names_from_inputs(inputs),
        'service_user': inputs.service_user,
        'service_user_credential_ref': _secret_ref('transit-user', inputs.foreign.internal_name),
        'reality_credentials_ref': _secret_ref('transit-reality', inputs.foreign.internal_name),
        'backup_outbounds': [],
    }
    config['routing'] = _ru_edge_routing_config()
    return config


def _ru_edge_config_from_foreign(inputs: ExtraRuEdgeInput) -> dict[str, Any]:
    foreign = inputs.foreign_config
    common = _common_from_foreign(foreign)
    config = _base_config(common, inputs.ru_edge, inputs.tariffs)
    config['role'] = 'ru-edge'
    config['warp'] = {'mode': 'disabled'}
    if inputs.reality_strategy == 'remote_dest':
        _apply_remote_dest_config(
            config,
            inputs.ru_edge,
            remote_dest_target=inputs.remote_dest_target,
            remote_dest_server_names=inputs.remote_dest_server_names,
        )
    config['transit'] = {
        'mode': 'vless_reality',
        'outbound_tag': 'TRANSIT_TO_FOREIGN',
        'foreign_exit_domain': _foreign_transit_endpoint_from_config(foreign, ru_edge_ipv6=inputs.ru_edge.public_ipv6),
        'foreign_exit_port': foreign.transit.listen_port,
        'server_names': foreign.effective_reality_server_names(),
        'service_user': foreign.transit.service_user,
        'service_user_credential_ref': foreign.transit.service_user_credential_ref,
        'reality_credentials_ref': foreign.transit.reality_credentials_ref,
        'backup_outbounds': [],
    }
    config['routing'] = _ru_edge_routing_config()
    return config


def _ru_edge_routing_config() -> dict[str, Any]:
    return {
        'ru_route': 'direct',
        'default_route': 'foreign_exit',
        'ru_dns': [
            'https://common.dot.dns.yandex.net/dns-query',
            'tcp://77.88.8.8:53',
            '77.88.8.1',
        ],
        'foreign_dns_via_transit': True,
    }


def _common_from_foreign(foreign: NodeConfig) -> CommonGenerationInput:
    return CommonGenerationInput(
        main_ipv4=foreign.main_server.ipv4,
        remnawave_api_url=str(foreign.main_server.remnawave_api_url).rstrip('/'),
        admin_allowlist=tuple(foreign.ssh.admin_allowlist),
        admin_user=foreign.ssh.admin_user,
        ssh_port=foreign.ssh.port,
        dns_api_token_ref=foreign.site.dns_api_token_ref or 'secrets/dns-api-token',
    )


def _validate_foreign_exit_for_extra_ru_edge(foreign: NodeConfig) -> None:
    if foreign.role != NodeRole.FOREIGN_EXIT:
        raise ConfigBuildError(f'foreign_config must have role=foreign-exit, got {foreign.role.value}')
    if foreign.transit.mode != TransitMode.VLESS_REALITY:
        raise ConfigBuildError('foreign_config must have transit.mode=vless_reality')
    required = {
        'transit.listen_port': foreign.transit.listen_port,
        'transit.service_user': foreign.transit.service_user,
        'transit.service_user_credential_ref': foreign.transit.service_user_credential_ref,
        'transit.reality_credentials_ref': foreign.transit.reality_credentials_ref,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ConfigBuildError(f'foreign_config is missing {", ".join(missing)}')


def _base_config(common: CommonGenerationInput, node: NodeGenerationInput, tariffs: TariffTargets) -> dict[str, Any]:
    domain = _effective_domain(node)
    return {
        'schema_version': 1,
        'role': '<filled-by-scenario>',
        'display': {
            'internal_name': node.internal_name,
            'name': node.display_name,
            'cabinet_override': None,
        },
        'country_code': node.country_code,
        'domain': domain,
        'public_ipv4': node.public_ipv4,
        'public_ipv6': node.public_ipv6,
        'domain_rotation': {
            'spare_domains': list(node.spare_domains or _default_spare_domains(domain)),
            'dns_ttl_seconds': 300,
        },
        'main_server': {
            'ipv4': common.main_ipv4,
            'remnawave_api_url': common.remnawave_api_url,
        },
        'ssh': {
            'port': common.ssh_port,
            'admin_user': common.admin_user,
            'admin_allowlist': list(common.admin_allowlist),
        },
        'remnanode': {
            'node_port': DEFAULT_NODE_PORT,
            'secret_key_ref': _secret_ref('remnanode', node.internal_name),
        },
        'xray': {
            'xtls_api_port': DEFAULT_XTLS_API_PORT,
            'config_profile_uuid': None,
            'public_inbound_uuid': _stable_uuid('public-inbound', node.internal_name),
        },
        'host': {
            'remark': None,
            'address': None,
            'port': DEFAULT_PUBLIC_PORT,
            'inbound_ref': 'public',
            'visibility': True,
        },
        'reality': {
            'strategy': 'local_decoy_site',
            'credentials_ref': _secret_ref('reality-public', node.internal_name),
            'public_port': DEFAULT_PUBLIC_PORT,
            'local_decoy_addr': '127.0.0.1',
            'local_decoy_port': 8443,
            'flow': 'xtls-rprx-vision',
            'server_names': [domain],
            **_xhttp_reality_transport(node.internal_name),
        },
        'site': {
            'engine': 'caddy',
            'template': 'simple-studio',
            'title': f'{node.display_name} Studio',
            'contact_email': f'info@{domain}',
            'certificate_mode': 'file',
            'certificate_source': 'external_acme_dns01',
            'certificate_ca': 'public',
            'dns_api_token_ref': common.dns_api_token_ref,
        },
        'bedolaga': {
            'internal_squad_name': f'loc-{_slugify(node.internal_name)}',
            'external_squad_name': f'ext-{_slugify(node.internal_name)}',
            'attach_to_tariff_slugs': list(tariffs.slugs),
            'attach_to_tariff_names': list(tariffs.names),
            'trial_eligible': tariffs.trial_eligible,
        },
    }


def _apply_remote_dest_ru_warp_config(config: dict[str, Any], inputs: RuWarpInput) -> None:
    _apply_remote_dest_config(
        config,
        inputs.node,
        remote_dest_target=inputs.remote_dest_target,
        remote_dest_server_names=inputs.remote_dest_server_names,
    )


def _apply_remote_dest_foreign_config(config: dict[str, Any], inputs: CascadeDirectInput) -> None:
    _apply_remote_dest_config(
        config,
        inputs.foreign,
        remote_dest_target=inputs.foreign_remote_dest_target,
        remote_dest_server_names=inputs.foreign_remote_dest_server_names,
    )


def _apply_remote_dest_ru_edge_config(config: dict[str, Any], inputs: CascadeDirectInput) -> None:
    _apply_remote_dest_config(
        config,
        inputs.ru_edge,
        remote_dest_target=inputs.ru_edge_remote_dest_target,
        remote_dest_server_names=inputs.ru_edge_remote_dest_server_names,
    )


def _apply_remote_dest_config(
    config: dict[str, Any],
    node: NodeGenerationInput,
    *,
    remote_dest_target: str,
    remote_dest_server_names: tuple[str, ...],
) -> None:
    server_names = tuple(remote_dest_server_names) or (_remote_dest_host(remote_dest_target),)
    config['host']['address'] = node.public_ipv4
    config['reality'] = {
        'strategy': 'remote_dest',
        'credentials_ref': _secret_ref('reality-public', node.internal_name),
        'public_port': DEFAULT_PUBLIC_PORT,
        'local_decoy_addr': '127.0.0.1',
        'local_decoy_port': 8443,
        'flow': 'xtls-rprx-vision',
        'server_names': list(server_names),
        'target': remote_dest_target,
        **_xhttp_reality_transport(node.internal_name),
    }
    config['reality']['xhttp']['host'] = server_names[0]
    config['site']['certificate_source'] = 'unused_remote_dest'
    config['site']['dns_api_token_ref'] = None


def _warp_config(internal_name: str) -> dict[str, Any]:
    return {
        'mode': 'xray_native',
        'outbound_tag': 'WARP_OUT',
        'reserved_source': 'warp_registration_client_id_b64',
        'reserved': None,
        'registration_ref': _secret_ref('warp', internal_name),
    }


def _default_spare_domains(domain: str) -> tuple[str, str]:
    label, separator, rest = domain.partition('.')
    if not separator:
        raise ConfigBuildError(f'cannot build spare domains from invalid domain {domain!r}')
    return (f'{label}-b.{rest}', f'{label}-c.{rest}')


def _xhttp_reality_transport(internal_name: str) -> dict[str, Any]:
    token = uuid.uuid5(uuid.NAMESPACE_URL, f'templar-node:xhttp:{internal_name}').hex[:8]
    return {
        'transport': 'xhttp',
        'xhttp': {
            'path': f'/assets/{token}/{_slugify(internal_name)}',
            'mode': 'auto',
            'extra': dict(DEFAULT_XHTTP_SERVER_EXTRA),
        },
    }


def _effective_domain(node: NodeGenerationInput) -> str:
    if node.domain:
        return node.domain
    return f'{_slugify(node.internal_name)}.node.invalid'


def _required_domain(node: NodeGenerationInput, label: str) -> str:
    if not node.domain:
        raise ConfigBuildError(f'{label} requires a node domain')
    return node.domain


def _remote_dest_host(target: str) -> str:
    host, separator, _port = target.strip().partition(':')
    if separator != ':':
        raise ConfigBuildError('remote_dest_target must have host:port shape')
    return host.lower()


def _secret_ref(prefix: str, internal_name: str) -> str:
    return f'secrets/{prefix}-{_slugify(internal_name)}'


def _file_stem(internal_name: str) -> str:
    return _slugify(internal_name)


def _slugify(value: str) -> str:
    normalized = re.sub(r'[^a-z0-9]+', '-', value.strip().lower())
    normalized = normalized.strip('-')
    if not normalized:
        raise ConfigBuildError(f'cannot slugify blank value {value!r}')
    return normalized


def _stable_uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'templar-node:{kind}:{key}'))
