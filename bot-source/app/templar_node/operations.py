"""High-level operator scenarios built from the lower onboarding layers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from app.templar_node.bedolaga import DatabaseBedolagaAdapter, LocalBedolagaAdapter
from app.templar_node.cloudflare import CloudflareUpsertError, upsert_node_dns_records
from app.templar_node.fake_env import FakeEnvironmentStore
from app.templar_node.layer1 import (
    Layer1Error,
    Layer1SshBootstrapOptions,
    run_layer1_local_bootstrap,
    run_layer1_ssh_bootstrap,
)
from app.templar_node.layer2a import Layer2aError, run_layer2a_pre_bootstrap
from app.templar_node.layer2b import Layer2bError, run_layer2b_post_bootstrap
from app.templar_node.remnawave import (
    HttpRemnaWaveAdapter,
    LocalRemnaWaveAdapter,
    RemnaWaveAdapterError,
    RemnaWaveControlPlaneAdapter,
)
from app.templar_node.remnawave_probe import RemnaWaveProbeAuth
from app.templar_node.routes import RouteOverrideError, RouteOverrideStore
from app.templar_node.schemas import NodeConfig, NodeRole, RealityStrategy
from app.templar_node.secrets import LocalSecretStore, SecretStoreError
from app.templar_node.state import NodeStateStore, StateStoreError
from app.templar_node.warp import (
    WarpRegistrationError,
    WarpRegistrationOptions,
    ensure_warp_registration_for_config,
)


OperatorAdapter = Literal['local', 'live']
ProgressReporter = Callable[[str], None]


class OperatorError(RuntimeError):
    """Raised when a high-level operator scenario cannot finish safely."""


@dataclass(frozen=True)
class OperatorContext:
    adapter: OperatorAdapter
    secrets_dir: Path
    state_dir: Path
    render_dir: Path
    env_dir: Path | None = None
    api_key_ref: str | None = None
    auth_type: str = 'api_key'
    caddy_token_ref: str | None = None
    timeout_seconds: int = 20
    verify_tls: bool = True
    cloudflare_api_token_ref: str | None = None
    skip_dns: bool = False
    no_auto_warp_register: bool = False
    warp_options: WarpRegistrationOptions = field(default_factory=WarpRegistrationOptions)
    admin_public_key_ref: str = 'secrets/ssh-admin-public-key'
    admin_private_key_ref: str | None = 'secrets/ssh-admin-private-key'
    root_private_key_ref: str | None = None
    allow_admin_ssh_bootstrap: bool = False
    issue_certificates: bool = True
    acme_email: str | None = None
    harden_ssh: bool = True
    start_services: bool = True
    resync_subscriptions: bool = True
    progress: ProgressReporter | None = None


@dataclass(frozen=True)
class OperatorResult:
    scenario: str
    nodes: tuple[str, ...]
    lines: tuple[str, ...]

    def to_lines(self) -> list[str]:
        return [
            f'Operator scenario: {self.scenario}',
            f'Nodes: {", ".join(self.nodes)}',
            *self.lines,
        ]


@dataclass(frozen=True)
class RouteApplyResult:
    path: Path
    node: str
    profile_uuid: str
    profile_update_key: str
    profile_update_status: str
    domains: tuple[str, ...]
    ips: tuple[str, ...]
    state_path: Path | None = None

    def to_lines(self) -> list[str]:
        lines = [
            f'Routing profile apply: {self.node}',
            f'Routes file: {self.path}',
            f'Config profile: {self.profile_uuid}',
            f'Override domains: {len(self.domains)}',
            f'Override IP/CIDR: {len(self.ips)}',
            f'Profile update: {self.profile_update_key} ({self.profile_update_status})',
        ]
        if self.state_path is not None:
            lines.append(f'State: {self.state_path}')
        return lines


def run_cascade_direct_operator(
    *,
    foreign_config: NodeConfig,
    ru_edge_config: NodeConfig,
    context: OperatorContext,
    foreign_root_password_ref: str | None = None,
    ru_root_password_ref: str | None = None,
    foreign_root_private_key_ref: str | None = None,
    ru_root_private_key_ref: str | None = None,
) -> OperatorResult:
    return _run_cascade_pair_operator(
        scenario='cascade-direct',
        foreign_config=foreign_config,
        ru_edge_config=ru_edge_config,
        context=context,
        foreign_root_password_ref=foreign_root_password_ref,
        ru_root_password_ref=ru_root_password_ref,
        foreign_root_private_key_ref=foreign_root_private_key_ref,
        ru_root_private_key_ref=ru_root_private_key_ref,
        foreign_title='foreign direct / foreign exit',
        ru_edge_title='RU cascade edge',
    )


def run_extra_ru_edge_operator(
    *,
    foreign_config: NodeConfig,
    ru_edge_config: NodeConfig,
    context: OperatorContext,
    foreign_root_password_ref: str | None = None,
    ru_root_password_ref: str | None = None,
    foreign_root_private_key_ref: str | None = None,
    ru_root_private_key_ref: str | None = None,
) -> OperatorResult:
    missing_sources = [
        source
        for source in (ru_edge_config.public_ipv4, ru_edge_config.public_ipv6)
        if source and source not in (foreign_config.transit.allow_from or [])
    ]
    if missing_sources:
        raise OperatorError(
            f'foreign config transit.allow_from must include {", ".join(missing_sources)}; '
            'regenerate it with generate ru-edge --updated-foreign-config before applying this scenario',
        )
    return _run_cascade_pair_operator(
        scenario='ru-edge-add',
        foreign_config=foreign_config,
        ru_edge_config=ru_edge_config,
        context=context,
        foreign_root_password_ref=foreign_root_password_ref,
        ru_root_password_ref=ru_root_password_ref,
        foreign_root_private_key_ref=foreign_root_private_key_ref,
        ru_root_private_key_ref=ru_root_private_key_ref,
        foreign_title='foreign exit refresh for extra RU edge',
        ru_edge_title='extra RU cascade edge',
    )


def _run_cascade_pair_operator(
    *,
    scenario: str,
    foreign_config: NodeConfig,
    ru_edge_config: NodeConfig,
    context: OperatorContext,
    foreign_root_password_ref: str | None,
    ru_root_password_ref: str | None,
    foreign_root_private_key_ref: str | None,
    ru_root_private_key_ref: str | None,
    foreign_title: str,
    ru_edge_title: str,
) -> OperatorResult:
    _require_role(foreign_config, NodeRole.FOREIGN_EXIT, label='foreign config')
    _require_role(ru_edge_config, NodeRole.RU_EDGE, label='RU edge config')
    lines: list[str] = []
    lines.extend(
        _run_node_chain(
            foreign_config,
            context=context,
            root_password_ref=foreign_root_password_ref,
            root_private_key_ref=foreign_root_private_key_ref,
            title=foreign_title,
        ),
    )
    lines.extend(
        _run_node_chain(
            ru_edge_config,
            context=context,
            root_password_ref=ru_root_password_ref,
            root_private_key_ref=ru_root_private_key_ref,
            title=ru_edge_title,
        ),
    )
    return OperatorResult(
        scenario=scenario,
        nodes=(foreign_config.display.internal_name, ru_edge_config.display.internal_name),
        lines=tuple(lines),
    )


def run_ru_direct_operator(
    *,
    config: NodeConfig,
    context: OperatorContext,
    root_password_ref: str | None = None,
    root_private_key_ref: str | None = None,
    require_remote_dest: bool = False,
) -> OperatorResult:
    _require_role(config, NodeRole.RU_WARP, label='RU direct config')
    expected_strategy = RealityStrategy.REMOTE_DEST if require_remote_dest else RealityStrategy.LOCAL_DECOY_SITE
    if config.reality.strategy != expected_strategy:
        raise OperatorError(
            f'{config.display.internal_name} uses reality.strategy={config.reality.strategy.value}; '
            f'this scenario requires {expected_strategy.value}',
        )
    scenario = 'ru-direct-remote' if require_remote_dest else 'ru-direct'
    lines = _run_node_chain(
        config,
        context=context,
        root_password_ref=root_password_ref,
        root_private_key_ref=root_private_key_ref,
        title=scenario,
    )
    return OperatorResult(scenario=scenario, nodes=(config.display.internal_name,), lines=tuple(lines))


def run_routing_add_operator(
    *,
    config: NodeConfig,
    routes_file: Path,
    domains: list[str],
    ips: list[str],
    comment: str | None = None,
    apply: bool = False,
    remnawave_adapter: RemnaWaveControlPlaneAdapter | None = None,
    state_store: NodeStateStore | None = None,
) -> OperatorResult:
    try:
        result = RouteOverrideStore(routes_file).add(config, domains=domains, ips=ips, comment=comment)
    except RouteOverrideError as exc:
        raise OperatorError(str(exc)) from exc
    lines = _section('routing override', result.to_lines())
    if apply:
        if remnawave_adapter is None:
            raise OperatorError('routing-add --apply requires a RemnaWave adapter')
        apply_result = apply_routing_overrides(
            config=config,
            routes_file=routes_file,
            remnawave_adapter=remnawave_adapter,
            state_store=state_store,
        )
        lines.extend(_section('routing profile apply', apply_result.to_lines()))
    return OperatorResult(
        scenario='routing-add',
        nodes=(config.display.internal_name,),
        lines=tuple(lines),
    )


def apply_routing_overrides(
    *,
    config: NodeConfig,
    routes_file: Path,
    remnawave_adapter: RemnaWaveControlPlaneAdapter,
    state_store: NodeStateStore | None = None,
) -> RouteApplyResult:
    _require_role(config, NodeRole.RU_EDGE, label='routing config')
    store = RouteOverrideStore(routes_file)
    try:
        overrides = store.get_for_node(config)
        if state_store is None:
            profile_uuid = _route_profile_uuid(config, discovered=None)
            profile_update = remnawave_adapter.ensure_profile_update(
                config,
                profile_uuid=profile_uuid,
                route_overrides=overrides,
            )
            state_path = None
        else:
            with state_store.control_plane_lock(f'routing-add:{config.display.internal_name}'):
                state = state_store.load_or_init(config)
                profile_uuid = _route_profile_uuid(config, discovered=state.discovered)
                profile_update = remnawave_adapter.ensure_profile_update(
                    config,
                    profile_uuid=profile_uuid,
                    route_overrides=overrides,
                )
                state.update_discovered(
                    {
                        'profile_update_key': profile_update.key,
                        'route_overrides_file': str(store.path),
                        'route_override_domains': len(overrides.domains),
                        'route_override_ips': len(overrides.ips),
                    },
                )
                state_path = state_store.save(state)
    except (RouteOverrideError, RemnaWaveAdapterError, StateStoreError) as exc:
        raise OperatorError(str(exc)) from exc
    return RouteApplyResult(
        path=store.path,
        node=config.display.internal_name,
        profile_uuid=profile_uuid,
        profile_update_key=profile_update.key,
        profile_update_status=profile_update.status,
        domains=overrides.domains,
        ips=overrides.ips,
        state_path=state_path,
    )


def _route_profile_uuid(config: NodeConfig, *, discovered: dict[str, object] | None) -> str:
    value = None
    if discovered is not None:
        value = discovered.get('config_profile_uuid')
    value = value or config.xray.config_profile_uuid
    if not value:
        raise OperatorError('routing apply requires config.xray.config_profile_uuid or state with discovered config_profile_uuid')
    return str(value)


def _run_node_chain(
    config: NodeConfig,
    *,
    context: OperatorContext,
    root_password_ref: str | None,
    root_private_key_ref: str | None,
    title: str,
) -> list[str]:
    if context.adapter == 'local':
        return _run_node_local(config, context=context, title=title)
    return _run_node_live(
        config,
        context=context,
        root_password_ref=root_password_ref,
        root_private_key_ref=root_private_key_ref,
        title=title,
    )


def _run_node_local(config: NodeConfig, *, context: OperatorContext, title: str) -> list[str]:
    if context.env_dir is None:
        raise OperatorError('--env-dir is required for operator --adapter local')
    secret_store = LocalSecretStore(context.secrets_dir)
    state_store = NodeStateStore(context.state_dir)
    env_store = FakeEnvironmentStore(context.env_dir)
    remnawave = LocalRemnaWaveAdapter(env_store, secret_store=secret_store)
    _status(context, f'{title}: local pre-bootstrap')
    lines = _section(
        f'{title}: local pre-bootstrap',
        _safe_layer2a(config, remnawave, secret_store, state_store, progress=context.progress).to_lines(),
    )
    _status(context, f'{title}: local bootstrap')
    lines.extend(
        _section(
            f'{title}: local bootstrap',
            _safe_layer1_local(config, secret_store=secret_store, state_store=state_store, context=context, env_store=env_store).to_lines(),
        ),
    )
    _status(context, f'{title}: local post-bootstrap')
    lines.extend(
        _section(
            f'{title}: local post-bootstrap',
            _safe_layer2b(
                config,
                remnawave,
                LocalBedolagaAdapter(env_store),
                state_store,
                progress=context.progress,
            ).to_lines(),
        ),
    )
    return lines


def _run_node_live(
    config: NodeConfig,
    *,
    context: OperatorContext,
    root_password_ref: str | None,
    root_private_key_ref: str | None,
    title: str,
) -> list[str]:
    if not context.api_key_ref:
        raise OperatorError('--api-key-ref is required for operator --adapter live')
    if not root_password_ref and not root_private_key_ref and not context.allow_admin_ssh_bootstrap:
        raise OperatorError(f'root password/private key secret ref is required for {config.display.internal_name}')

    secret_store = LocalSecretStore(context.secrets_dir)
    state_store = NodeStateStore(context.state_dir)
    lines: list[str] = []
    if not context.no_auto_warp_register:
        _status(context, f'{title}: WARP registration check')
        warp = _safe_warp_register(config, secret_store=secret_store, context=context)
        if warp:
            lines.extend(_section(f'{title}: WARP registration', warp.to_lines()))

    _status(context, f'{title}: pre-bootstrap RemnaWave objects')
    pre_remnawave = _http_remnawave(config, context=context, secret_store=secret_store)
    lines.extend(
        _section(
            f'{title}: pre-bootstrap',
            _safe_layer2a(config, pre_remnawave, secret_store, state_store, progress=context.progress).to_lines(),
        ),
    )

    _status(context, f'{title}: Cloudflare DNS records')
    dns_lines = _maybe_upsert_dns(config, context=context, secret_store=secret_store)
    if dns_lines:
        lines.extend(_section(f'{title}: DNS upsert', dns_lines))

    _status(context, f'{title}: SSH bootstrap on VPS')
    lines.extend(
        _section(
            f'{title}: SSH bootstrap',
            _safe_layer1_ssh(
                config,
                secret_store=secret_store,
                state_store=state_store,
                context=context,
                root_password_ref=root_password_ref,
                root_private_key_ref=root_private_key_ref,
            ).to_lines(),
        ),
    )

    _status(context, f'{title}: post-bootstrap RemnaWave/Bedolaga sync')
    post_remnawave = _http_remnawave(config, context=context, secret_store=secret_store)
    lines.extend(
        _section(
            f'{title}: post-bootstrap',
            _safe_layer2b(
                config,
                post_remnawave,
                DatabaseBedolagaAdapter(resync_subscriptions=context.resync_subscriptions),
                state_store,
                progress=context.progress,
                node_online_timeout_seconds=120,
                node_online_interval_seconds=10,
            ).to_lines(),
        ),
    )
    return lines


def _safe_warp_register(config: NodeConfig, *, secret_store: LocalSecretStore, context: OperatorContext):
    try:
        return ensure_warp_registration_for_config(
            config,
            secret_store=secret_store,
            options=context.warp_options,
            overwrite=False,
        )
    except (WarpRegistrationError, SecretStoreError, ValueError) as exc:
        raise OperatorError(str(exc)) from exc


def _safe_layer2a(
    config: NodeConfig,
    remnawave,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    *,
    progress: ProgressReporter | None = None,
):
    try:
        return run_layer2a_pre_bootstrap(
            config,
            adapter=remnawave,
            secret_store=secret_store,
            state_store=state_store,
            progress=progress,
        )
    except Layer2aError as exc:
        raise OperatorError(str(exc)) from exc


def _safe_layer1_local(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    context: OperatorContext,
    env_store: FakeEnvironmentStore,
):
    try:
        return run_layer1_local_bootstrap(
            config,
            secret_store=secret_store,
            state_store=state_store,
            output_dir=context.render_dir,
            env_store=env_store,
            progress=context.progress,
        )
    except Layer1Error as exc:
        raise OperatorError(str(exc)) from exc


def _safe_layer1_ssh(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    context: OperatorContext,
    root_password_ref: str | None,
    root_private_key_ref: str | None,
):
    def run_with(root_ref: str | None, root_key_ref: str | None):
        return run_layer1_ssh_bootstrap(
            config,
            secret_store=secret_store,
            state_store=state_store,
            output_dir=context.render_dir,
            options=Layer1SshBootstrapOptions(
                root_password_ref=root_ref,
                root_private_key_ref=root_key_ref,
                admin_public_key_ref=context.admin_public_key_ref,
                admin_private_key_ref=context.admin_private_key_ref,
                dns_api_token_ref=context.cloudflare_api_token_ref,
                acme_email=context.acme_email,
                issue_certificates=context.issue_certificates,
                harden_ssh=context.harden_ssh,
                start_services=context.start_services,
                progress=context.progress,
            ),
        )

    try:
        return run_with(root_password_ref, root_private_key_ref)
    except Layer1Error as exc:
        if (root_password_ref or root_private_key_ref) and context.allow_admin_ssh_bootstrap and context.admin_private_key_ref:
            _status(
                context,
                f'{config.display.internal_name}: root SSH failed; retrying bootstrap with existing admin SSH key',
            )
            try:
                return run_with(None, None)
            except Layer1Error as retry_exc:
                raise OperatorError(f'{exc}; admin-key retry failed: {retry_exc}') from retry_exc
        raise OperatorError(str(exc)) from exc


def _safe_layer2b(
    config: NodeConfig,
    remnawave,
    bedolaga,
    state_store: NodeStateStore,
    *,
    progress: ProgressReporter | None = None,
    node_online_timeout_seconds: int = 0,
    node_online_interval_seconds: int = 10,
):
    try:
        return run_layer2b_post_bootstrap(
            config,
            remnawave_adapter=remnawave,
            bedolaga_adapter=bedolaga,
            state_store=state_store,
            progress=progress,
            node_online_timeout_seconds=node_online_timeout_seconds,
            node_online_interval_seconds=node_online_interval_seconds,
        )
    except Layer2bError as exc:
        raise OperatorError(str(exc)) from exc


def _status(context: OperatorContext, message: str) -> None:
    if context.progress is not None:
        context.progress(message)


def _maybe_upsert_dns(config: NodeConfig, *, context: OperatorContext, secret_store: LocalSecretStore) -> list[str]:
    if context.skip_dns:
        return ['skipped by --skip-dns']
    if config.reality.strategy != RealityStrategy.LOCAL_DECOY_SITE:
        return ['skipped: remote_dest strategy does not use node-domain DNS']
    api_token_ref = context.cloudflare_api_token_ref or config.site.dns_api_token_ref
    if not api_token_ref:
        raise OperatorError('DNS upsert requires --cloudflare-api-token-ref or site.dns_api_token_ref')
    try:
        api_token = secret_store.read_text(api_token_ref)
        lines = upsert_node_dns_records(
            fqdn=config.domain,
            ipv4=config.public_ipv4,
            ipv6=_public_dns_ipv6(config),
            ttl=config.domain_rotation.dns_ttl_seconds,
            api_token=api_token,
            timeout_seconds=context.timeout_seconds,
            proxied=False,
        ).to_lines()
        host_dns_name = _host_override_dns_name(config)
        if host_dns_name and config.public_ipv6:
            lines.extend(
                upsert_node_dns_records(
                    fqdn=host_dns_name,
                    ipv4=None,
                    ipv6=config.public_ipv6,
                    ttl=config.domain_rotation.dns_ttl_seconds,
                    api_token=api_token,
                    timeout_seconds=context.timeout_seconds,
                    proxied=False,
                ).to_lines(),
            )
        return lines
    except (CloudflareUpsertError, SecretStoreError, ValueError) as exc:
        raise OperatorError(str(exc)) from exc


def _public_dns_ipv6(config: NodeConfig) -> str | None:
    if config.public_ipv4:
        return None
    return config.public_ipv6


def _host_override_dns_name(config: NodeConfig) -> str | None:
    address = config.host.address
    if not address or address == config.domain:
        return None
    try:
        ip_address(address)
    except ValueError:
        return address
    return None


def _http_remnawave(config: NodeConfig, *, context: OperatorContext, secret_store: LocalSecretStore) -> HttpRemnaWaveAdapter:
    if not context.api_key_ref:
        raise OperatorError('--api-key-ref is required for live RemnaWave operations')
    try:
        api_key = secret_store.read_text(context.api_key_ref)
        caddy_token = secret_store.read_text(context.caddy_token_ref) if context.caddy_token_ref else None
    except SecretStoreError as exc:
        raise OperatorError(str(exc)) from exc
    return HttpRemnaWaveAdapter(
        api_url=str(config.main_server.remnawave_api_url),
        auth=RemnaWaveProbeAuth(api_key=api_key, auth_type=context.auth_type, caddy_token=caddy_token),
        secret_store=secret_store,
        timeout_seconds=context.timeout_seconds,
        verify_tls=context.verify_tls,
    )


def _require_role(config: NodeConfig, role: NodeRole, *, label: str) -> None:
    if config.role != role:
        raise OperatorError(f'{label} must have role={role.value}, got {config.role.value}')


def _section(title: str, lines: list[str]) -> list[str]:
    return ['', f'== {title} ==', *lines]
