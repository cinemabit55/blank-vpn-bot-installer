"""Manual decommission flow for a Templar node connection."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.templar_node.bedolaga import BedolagaAdapterError, BedolagaDecommissionResult
from app.templar_node.cloudflare import CloudflareUpsertError, delete_node_dns_records
from app.templar_node.layer1 import sh_quote
from app.templar_node.remnawave import RemnaWaveAdapterError, RemnaWaveDecommissionResult
from app.templar_node.schemas import NodeConfig, NodeRole, RealityStrategy
from app.templar_node.secrets import LocalSecretStore, SecretStoreError
from app.templar_node.state import NodeStateStore, StateStoreError


class DecommissionError(RuntimeError):
    """Raised when a node cannot be decommissioned safely."""


class RemnaWaveDecommissionAdapter(Protocol):
    def decommission_resources(
        self,
        config: NodeConfig,
        *,
        discovered: dict[str, Any],
        delete_transit_service_user: bool,
        dry_run: bool,
    ) -> RemnaWaveDecommissionResult:
        """Delete or plan deletion of RemnaWave resources for a node."""


class BedolagaDecommissionAdapter(Protocol):
    def detach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> BedolagaDecommissionResult:
        """Detach node squads from Bedolaga tariffs/subscriptions/server list."""


@dataclass(frozen=True)
class LocalCleanupResult:
    actions: tuple[str, ...]

    def to_lines(self) -> list[str]:
        if not self.actions:
            return ['Local cleanup: nothing selected']
        return ['Local cleanup:', *self.actions]


@dataclass(frozen=True)
class SshCleanupResult:
    host: str
    actions: tuple[str, ...]
    output: str = ''

    def to_lines(self) -> list[str]:
        lines = [f'SSH cleanup: {self.host}', *self.actions]
        if self.output:
            lines.append('Remote output:')
            lines.extend(self.output.strip().splitlines())
        return lines


@dataclass(frozen=True)
class DecommissionResult:
    internal_name: str
    dry_run: bool
    remnawave: RemnaWaveDecommissionResult | None
    bedolaga: BedolagaDecommissionResult | None
    dns: tuple[str, ...]
    local: LocalCleanupResult
    ssh: SshCleanupResult | None

    def to_lines(self) -> list[str]:
        mode = 'dry-run' if self.dry_run else 'applied'
        lines = [f'Decommission: {self.internal_name} ({mode})']
        if self.remnawave is not None:
            lines.extend(['', *self.remnawave.to_lines()])
        if self.bedolaga is not None:
            lines.extend(['', *self.bedolaga.to_lines()])
        if self.dns:
            lines.extend(['', 'DNS cleanup:', *self.dns])
        lines.extend(['', *self.local.to_lines()])
        if self.ssh is not None:
            lines.extend(['', *self.ssh.to_lines()])
        if self.dry_run:
            lines.extend(['', 'Dry run only. Re-run with --yes to apply these deletions.'])
        return lines


def run_decommission(
    config: NodeConfig,
    *,
    state_store: NodeStateStore,
    remnawave_adapter: RemnaWaveDecommissionAdapter | None = None,
    bedolaga_adapter: BedolagaDecommissionAdapter | None = None,
    secret_store: LocalSecretStore | None = None,
    render_dir: Path | None = None,
    routes_file: Path | None = None,
    monitor_config: Path | None = None,
    config_file: Path | None = None,
    delete_dns: bool = False,
    cloudflare_api_token_ref: str | None = None,
    delete_local_files: bool = False,
    delete_config_file: Path | None = None,
    delete_secrets: bool = False,
    delete_transit_service_user: bool = False,
    ssh_cleanup: bool = False,
    ssh_private_key_ref: str | None = None,
    ssh_root_password_ref: str | None = None,
    disable_empty_monitor: bool = False,
    internal_squad_uuid_override: str | None = None,
    external_squad_uuid_override: str | None = None,
    dry_run: bool = True,
    ssh_timeout_seconds: int = 30,
) -> DecommissionResult:
    try:
        with state_store.control_plane_lock(f'decommission:{config.display.internal_name}'):
            state = state_store.load(config.display.internal_name)
            discovered = dict(state.discovered) if state is not None else {}
            if internal_squad_uuid_override:
                discovered['internal_squad_uuid'] = internal_squad_uuid_override
            if external_squad_uuid_override:
                discovered['external_squad_uuid'] = external_squad_uuid_override
            internal_squad_uuid = _discovered(discovered, 'internal_squad_uuid')
            external_squad_uuid = _discovered(discovered, 'external_squad_uuid')

            remnawave_result = None
            if remnawave_adapter is not None:
                remnawave_result = remnawave_adapter.decommission_resources(
                    config,
                    discovered=discovered,
                    delete_transit_service_user=delete_transit_service_user,
                    dry_run=dry_run,
                )

            bedolaga_result = None
            if bedolaga_adapter is not None:
                bedolaga_result = bedolaga_adapter.detach_squads(
                    config,
                    internal_squad_uuid=internal_squad_uuid,
                    external_squad_uuid=external_squad_uuid,
                    dry_run=dry_run,
                )

            dns_lines = _cleanup_dns(
                config,
                secret_store=secret_store,
                api_token_ref=cloudflare_api_token_ref,
                enabled=delete_dns,
                dry_run=dry_run,
            )
            ssh_result = _cleanup_ssh(
                config,
                secret_store=secret_store,
                enabled=ssh_cleanup,
                private_key_ref=ssh_private_key_ref,
                root_password_ref=ssh_root_password_ref,
                dry_run=dry_run,
                timeout_seconds=ssh_timeout_seconds,
            )
            local_result = _cleanup_local(
                config,
                state_store=state_store,
                secret_store=secret_store,
                render_dir=render_dir,
                routes_file=routes_file,
                monitor_config=monitor_config,
                config_file=config_file,
                delete_config_file=delete_config_file,
                delete_local_files=delete_local_files,
                delete_secrets=delete_secrets,
                delete_transit_service_user=delete_transit_service_user,
                disable_empty_monitor=disable_empty_monitor,
                dry_run=dry_run,
            )
    except (RemnaWaveAdapterError, BedolagaAdapterError, SecretStoreError, StateStoreError, CloudflareUpsertError, OSError, subprocess.SubprocessError) as exc:
        raise DecommissionError(str(exc)) from exc
    return DecommissionResult(
        internal_name=config.display.internal_name,
        dry_run=dry_run,
        remnawave=remnawave_result,
        bedolaga=bedolaga_result,
        dns=tuple(dns_lines),
        local=local_result,
        ssh=ssh_result,
    )


def _cleanup_dns(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore | None,
    api_token_ref: str | None,
    enabled: bool,
    dry_run: bool,
) -> list[str]:
    if not enabled:
        return []
    if config.reality.strategy != RealityStrategy.LOCAL_DECOY_SITE:
        return ['SKIP remote_dest strategy has no node-domain DNS record']
    if dry_run:
        records = ['A'] if config.public_ipv4 else []
        if config.public_ipv6:
            records.append('AAAA')
        lines = [f'WOULD DELETE {record_type} {config.domain}' for record_type in records]
        host_dns_name = _host_override_dns_name(config)
        if host_dns_name and config.public_ipv6:
            lines.append(f'WOULD DELETE AAAA {host_dns_name}')
        return lines
    if secret_store is None:
        raise DecommissionError('--delete-dns requires --secrets-dir')
    ref = api_token_ref or config.site.dns_api_token_ref
    if not ref:
        raise DecommissionError('--delete-dns requires --cloudflare-api-token-ref or site.dns_api_token_ref')
    api_token = secret_store.read_text(ref)
    lines = delete_node_dns_records(
        fqdn=config.domain,
        ipv4=config.public_ipv4,
        ipv6=config.public_ipv6,
        api_token=api_token,
    ).to_lines()
    host_dns_name = _host_override_dns_name(config)
    if host_dns_name and config.public_ipv6:
        lines.extend(
            delete_node_dns_records(
                fqdn=host_dns_name,
                ipv4=None,
                ipv6=config.public_ipv6,
                api_token=api_token,
            ).to_lines(),
        )
    return lines


def _host_override_dns_name(config: NodeConfig) -> str | None:
    address = config.host.address
    if not address or address == config.domain:
        return None
    try:
        ip_address(address)
    except ValueError:
        return address
    return None


def _cleanup_local(
    config: NodeConfig,
    *,
    state_store: NodeStateStore,
    secret_store: LocalSecretStore | None,
    render_dir: Path | None,
    routes_file: Path | None,
    monitor_config: Path | None,
    config_file: Path | None,
    delete_config_file: Path | None,
    delete_local_files: bool,
    delete_secrets: bool,
    delete_transit_service_user: bool,
    disable_empty_monitor: bool,
    dry_run: bool,
) -> LocalCleanupResult:
    actions: list[str] = []
    if delete_local_files:
        state_dir = state_store.node_dir(config.display.internal_name)
        actions.append(_delete_path(state_dir, dry_run=dry_run, label='state dir'))
        if render_dir is not None:
            actions.append(_delete_path(Path(render_dir) / config.display.internal_name, dry_run=dry_run, label='render dir'))
        if routes_file is not None:
            actions.append(_remove_routes_node(routes_file, config, dry_run=dry_run))
        if monitor_config is not None:
            actions.append(_remove_monitor_checks(
                monitor_config,
                config,
                config_file=config_file or delete_config_file,
                disable_when_empty=disable_empty_monitor,
                dry_run=dry_run,
            ))
        if delete_config_file is not None:
            actions.append(_delete_path(delete_config_file, dry_run=dry_run, label='config file'))
    if delete_secrets:
        for ref in _owned_secret_refs(config, delete_transit_service_user=delete_transit_service_user):
            if dry_run:
                actions.append(f'WOULD DELETE secret: {ref}')
                continue
            if secret_store is None:
                raise DecommissionError('--delete-secrets requires --secrets-dir')
            actions.append(_delete_secret(secret_store, ref, dry_run=dry_run))
    return LocalCleanupResult(tuple(actions))


def _cleanup_ssh(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore | None,
    enabled: bool,
    private_key_ref: str | None,
    root_password_ref: str | None,
    dry_run: bool,
    timeout_seconds: int,
) -> SshCleanupResult | None:
    if not enabled:
        return None
    actions = (
        'stop/removes /opt/remnanode Docker stack',
        'removes /opt/remnanode, /opt/templar-node, /opt/node-site',
        'removes generated Caddy config/certs and reloads Caddy if active',
        'deletes node-specific UFW rules for public/node/transit ports',
    )
    if dry_run:
        return SshCleanupResult(host=config.public_ipv4, actions=tuple(f'WOULD {action}' for action in actions))
    if secret_store is None or (not private_key_ref and not root_password_ref):
        raise DecommissionError('--ssh-cleanup requires --secrets-dir and --admin-private-key-ref or --root-password-ref')

    errors: list[str] = []
    if private_key_ref:
        try:
            private_key = secret_store.read_text(private_key_ref)
            output = _run_admin_ssh(
                config,
                private_key,
                _remote_cleanup_script(config),
                timeout_seconds=timeout_seconds,
            )
            return SshCleanupResult(host=config.public_ipv4, actions=tuple(f'DONE {action}' for action in actions), output=output)
        except (SecretStoreError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f'admin-key SSH failed: {exc}')

    if root_password_ref:
        try:
            output = _run_root_password_ssh(
                config,
                secret_store.path_for_ref(root_password_ref),
                _remote_cleanup_script(config),
                timeout_seconds=timeout_seconds,
            )
            return SshCleanupResult(host=config.public_ipv4, actions=tuple(f'DONE {action}' for action in actions), output=output)
        except (SecretStoreError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f'root-password SSH failed: {exc}')

    detail = '; '.join(errors) or '--ssh-cleanup could not authenticate'
    return SshCleanupResult(
        host=config.public_ipv4,
        actions=tuple(f'SKIPPED {action}' for action in actions),
        output='SSH cleanup failed; control-plane cleanup continued. ' + detail,
    )


def _run_root_password_ssh(config: NodeConfig, root_password_path: Path, script: str, *, timeout_seconds: int) -> str:
    sshpass_path = shutil.which('sshpass')
    ssh_path = shutil.which('ssh')
    if sshpass_path is None or ssh_path is None:
        raise DecommissionError('sshpass and ssh are required for root password --ssh-cleanup fallback')
    completed = subprocess.run(  # noqa: S603
        [
            sshpass_path,
            '-f',
            str(root_password_path),
            ssh_path,
            '-p',
            str(config.ssh.port),
            '-o',
            'StrictHostKeyChecking=accept-new',
            f'root@{config.public_ipv4}',
            'bash -s',
        ],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout_seconds,
    )
    return completed.stdout


def _run_admin_ssh(config: NodeConfig, private_key: str, script: str, *, timeout_seconds: int) -> str:
    ssh_path = shutil.which('ssh')
    if ssh_path is None:
        raise DecommissionError('ssh is required for --ssh-cleanup')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as key_file:
        key_file.write(private_key.rstrip() + '\n')
        key_path = Path(key_file.name)
    try:
        key_path.chmod(0o600)
        completed = subprocess.run(  # noqa: S603
            [
                ssh_path,
                '-i',
                str(key_path),
                '-p',
                str(config.ssh.port),
                '-o',
                'BatchMode=yes',
                '-o',
                'IdentitiesOnly=yes',
                '-o',
                'StrictHostKeyChecking=accept-new',
                f'{config.ssh.admin_user}@{config.public_ipv4}',
                'sudo bash -s',
            ],
            input=script,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout_seconds,
        )
        return completed.stdout
    finally:
        key_path.unlink(missing_ok=True)


def _remote_cleanup_script(config: NodeConfig) -> str:
    ufw_lines = [
        f'ufw --force delete allow {int(config.reality.public_port)}/tcp || true',
        f'ufw --force delete allow from {sh_quote(config.main_server.ipv4)} to any port {int(config.remnanode.node_port)} proto tcp || true',
    ]
    if config.role == NodeRole.FOREIGN_EXIT and config.transit.listen_port:
        if config.host.inbound_ref == 'transit':
            ufw_lines.append(f'ufw --force delete allow {int(config.transit.listen_port)}/tcp || true')
        for source in config.transit.allow_from or []:
            ufw_lines.append(
                f'ufw --force delete allow from {sh_quote(source)} to any port {int(config.transit.listen_port)} proto tcp || true',
            )
    return f"""set -euo pipefail
if [ -d /opt/remnanode ]; then
  (cd /opt/remnanode && docker compose down --remove-orphans) || true
fi
rm -rf /opt/remnanode /opt/templar-node /opt/node-site {sh_quote('/var/lib/templar-node-bootstrap/' + config.display.internal_name)}
rm -f /etc/caddy/Caddyfile /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem
if systemctl is-active --quiet caddy; then
  systemctl reload caddy || systemctl restart caddy || true
fi
{chr(10).join(ufw_lines)}
echo 'templar-node remote cleanup completed'
"""


def _delete_path(path: Path, *, dry_run: bool, label: str) -> str:
    path = Path(path).expanduser().resolve()
    if dry_run:
        return f'WOULD DELETE {label}: {path}'
    if path.is_dir():
        shutil.rmtree(path)
        return f'DELETED {label}: {path}'
    if path.exists():
        path.unlink()
        return f'DELETED {label}: {path}'
    return f'MISSING {label}: {path}'


def _delete_secret(secret_store: LocalSecretStore, ref: str, *, dry_run: bool) -> str:
    if dry_run:
        return f'WOULD DELETE secret: {ref}'
    deleted = secret_store.delete_ref(ref)
    return f'DELETED secret: {ref}' if deleted else f'MISSING secret: {ref}'


def _owned_secret_refs(config: NodeConfig, *, delete_transit_service_user: bool) -> list[str]:
    refs = [config.remnanode.secret_key_ref, config.reality.credentials_ref]
    if config.warp.registration_ref:
        refs.append(config.warp.registration_ref)
    if config.role == NodeRole.FOREIGN_EXIT:
        if config.transit.reality_credentials_ref:
            refs.append(config.transit.reality_credentials_ref)
        if delete_transit_service_user and config.transit.service_user_credential_ref:
            refs.append(config.transit.service_user_credential_ref)
    return [ref for ref in dict.fromkeys(refs) if ref]


def _remove_routes_node(path: Path, config: NodeConfig, *, dry_run: bool) -> str:
    path = Path(path).expanduser().resolve()
    if dry_run:
        return f'WOULD REMOVE route overrides for {config.display.internal_name}: {path}'
    if not path.exists():
        return f'MISSING routes file: {path}'
    raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    nodes = raw.get('nodes') if isinstance(raw, dict) else None
    if not isinstance(nodes, dict) or config.display.internal_name not in nodes:
        return f'NO ROUTES for {config.display.internal_name}: {path}'
    del nodes[config.display.internal_name]
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return f'REMOVED route overrides for {config.display.internal_name}: {path}'


def _remove_monitor_checks(
    path: Path,
    config: NodeConfig,
    *,
    config_file: Path | None,
    disable_when_empty: bool,
    dry_run: bool,
) -> str:
    path = Path(path).expanduser().resolve()
    if dry_run:
        return f'WOULD REMOVE monitor checks referencing {config.display.internal_name}: {path}'
    if not path.exists():
        return f'MISSING monitor config: {path}'
    raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    checks = raw.get('checks') if isinstance(raw, dict) else None
    if not isinstance(checks, list):
        return f'NO monitor checks list: {path}'
    config_name = config.display.internal_name
    domain = config.domain
    kept: list[Any] = []
    removed_ids: list[str] = []
    for item in checks:
        if _monitor_check_matches(item, config_name=config_name, domain=domain, config_file=config_file):
            check_id = item.get('id') if isinstance(item, dict) else None
            if check_id:
                removed_ids.append(str(check_id))
            continue
        kept.append(item)
    raw['checks'] = kept
    removed = len(checks) - len(kept)
    if removed:
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding='utf-8')
    state_removed = _remove_monitor_state_records(raw, removed_ids)
    timer_status = ''
    if removed and disable_when_empty and not raw['checks']:
        timer_status = f'; {_disable_monitor_timer()}'
    return f'REMOVED monitor checks: {removed}; state records: {state_removed}{timer_status} from {path}'


def _disable_monitor_timer() -> str:
    systemctl = shutil.which('systemctl')
    if systemctl is None:
        return 'monitor timer not disabled: systemctl missing'
    command = [systemctl, 'stop', 'templar-node-monitor.timer', 'templar-node-monitor.service']
    disable_command = [systemctl, 'disable', 'templar-node-monitor.timer']
    stop = subprocess.run(command, text=True, capture_output=True, check=False)  # noqa: S603
    disable = subprocess.run(disable_command, text=True, capture_output=True, check=False)  # noqa: S603
    if stop.returncode == 0 and disable.returncode == 0:
        return 'monitor timer disabled'
    detail = (stop.stderr or disable.stderr or stop.stdout or disable.stdout).strip()
    return f'monitor timer disable attempted: {detail}' if detail else 'monitor timer disable attempted'


def _remove_monitor_state_records(raw: dict[str, Any], removed_ids: list[str]) -> int:
    if not removed_ids:
        return 0
    monitor = raw.get('monitor') if isinstance(raw, dict) else None
    state_file_value = monitor.get('state_file') if isinstance(monitor, dict) else None
    if not state_file_value:
        return 0
    state_path = Path(str(state_file_value)).expanduser().resolve()
    if not state_path.exists():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return 0
    checks = state.get('checks') if isinstance(state, dict) else None
    if not isinstance(checks, dict):
        return 0
    removed = 0
    for check_id in removed_ids:
        if check_id in checks:
            del checks[check_id]
            removed += 1
    if removed:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return removed


def _monitor_check_matches(item: Any, *, config_name: str, domain: str, config_file: Path | None) -> bool:
    if not isinstance(item, dict):
        return False
    haystack = ' '.join(str(value) for value in item.values() if isinstance(value, (str, Path)))
    normalized_haystack = _safe_lower(haystack)
    if config_name in haystack or _safe_lower(config_name) in normalized_haystack:
        return True
    if domain and (domain in haystack or _safe_lower(domain) in normalized_haystack):
        return True
    if _safe_lower(config_name) in _safe_lower(str(item.get('id') or '')):
        return True
    if config_file is None:
        return False
    config_path = Path(config_file).expanduser()
    config_refs = {str(config_path), config_path.name}
    try:
        config_refs.add(str(config_path.resolve()))
    except OSError:
        pass
    return any(ref and (ref in haystack or _safe_lower(ref) in normalized_haystack) for ref in config_refs)


def _safe_lower(value: str) -> str:
    return value.lower().replace('_', '-').replace(' ', '-')


def _discovered(discovered: dict[str, Any], key: str) -> str | None:
    value = discovered.get(key)
    return str(value) if value else None
