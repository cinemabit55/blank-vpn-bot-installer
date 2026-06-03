"""Controlled node-domain rotation preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from app.templar_node.schemas import DOMAIN_RE, NodeConfig, NodeRole, RealityStrategy
from app.templar_node.secrets import LocalSecretStore, SecretStoreError
from app.templar_node.state import NodeStateStore, utc_now_iso


class DomainRotationError(ValueError):
    """Raised when a node-domain rotation cannot be prepared safely."""


@dataclass(frozen=True)
class RealitySecretRotationResult:
    ref: str
    path: Path
    backup_path: Path | None
    status: str
    server_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            'ref': self.ref,
            'path': str(self.path),
            'backup_path': str(self.backup_path) if self.backup_path else None,
            'status': self.status,
            'server_names': list(self.server_names),
        }

    def to_lines(self) -> list[str]:
        lines = [f'REALITY secret: {self.ref} ({self.status})']
        lines.append(f'Path: {self.path}')
        if self.backup_path:
            lines.append(f'Backup: {self.backup_path}')
        lines.append(f'Server names: {", ".join(self.server_names)}')
        return lines


@dataclass(frozen=True)
class DomainRotationSwitchStateResult:
    path: Path
    status: str
    rollback: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'path': str(self.path),
            'status': self.status,
            'rollback': self.rollback,
            'error': self.error,
        }

    def to_lines(self) -> list[str]:
        lines = [f'Domain rotation switch state: {self.status}', f'State: {self.path}']
        if self.error:
            lines.append(f'Error: {self.error}')
        lines.append(f'Rollback old domain: {self.rollback.get("old_domain", "<unknown>")}')
        lines.append(f'Rollback new domain: {self.rollback.get("new_domain", "<unknown>")}')
        return lines


@dataclass(frozen=True)
class DomainRotationPlan:
    internal_name: str
    role: str
    old_domain: str
    new_domain: str
    old_host_address: str
    new_host_address: str
    old_spare_domains: tuple[str, ...]
    new_spare_domains: tuple[str, ...]
    rotated_config: NodeConfig
    warnings: tuple[str, ...]

    @property
    def requires_reality_secret_update(self) -> bool:
        return self.rotated_config.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE

    def to_dict(self, *, config_path: Path | None = None) -> dict[str, Any]:
        return {
            'internal_name': self.internal_name,
            'role': self.role,
            'old_domain': self.old_domain,
            'new_domain': self.new_domain,
            'old_host_address': self.old_host_address,
            'new_host_address': self.new_host_address,
            'old_spare_domains': list(self.old_spare_domains),
            'new_spare_domains': list(self.new_spare_domains),
            'requires_reality_secret_update': self.requires_reality_secret_update,
            'warnings': list(self.warnings),
            'config_path': str(config_path) if config_path else None,
        }

    def to_lines(self, *, config_path: Path | None = None) -> list[str]:
        lines = [
            f'Domain rotation prepare: {self.internal_name}',
            f'Role: {self.role}',
            f'Current domain: {self.old_domain}',
            f'New domain: {self.new_domain}',
            f'Host address: {self.old_host_address} -> {self.new_host_address}',
            f'New spare domains: {", ".join(self.new_spare_domains)}',
            f'Rotated config: {config_path or "not written"}',
        ]
        if self.requires_reality_secret_update:
            ref = self.rotated_config.reality.credentials_ref or '<missing>'
            names = ', '.join(self.rotated_config.effective_reality_server_names())
            lines.append(f'REALITY serverNames required in {ref}: {names}')
        lines.extend(f'Warning: {warning}' for warning in self.warnings)
        return lines


@dataclass(frozen=True)
class SniChangePlan:
    internal_name: str
    role: str
    old_target: str | None
    new_target: str
    old_server_names: tuple[str, ...]
    new_server_names: tuple[str, ...]
    old_xhttp_host: str | None
    new_xhttp_host: str | None
    changed_config: NodeConfig

    def to_dict(self, *, config_path: Path | None = None) -> dict[str, Any]:
        return {
            'internal_name': self.internal_name,
            'role': self.role,
            'old_target': self.old_target,
            'new_target': self.new_target,
            'old_server_names': list(self.old_server_names),
            'new_server_names': list(self.new_server_names),
            'old_xhttp_host': self.old_xhttp_host,
            'new_xhttp_host': self.new_xhttp_host,
            'config_path': str(config_path) if config_path else None,
        }

    def to_lines(self, *, config_path: Path | None = None) -> list[str]:
        return [
            f'SNI change: {self.internal_name}',
            f'Role: {self.role}',
            f'REALITY target: {self.old_target or "<empty>"} -> {self.new_target}',
            f'Server names: {", ".join(self.old_server_names) or "<empty>"} -> {", ".join(self.new_server_names)}',
            f'XHTTP host: {self.old_xhttp_host or "<empty>"} -> {self.new_xhttp_host or "<empty>"}',
            f'Changed config: {config_path or "not written"}',
        ]


def build_domain_rotation_plan(
    config: NodeConfig,
    *,
    to_domain: str,
    allow_custom_domain: bool = False,
) -> DomainRotationPlan:
    """Build a validated rotated config without mutating files or remote services."""

    if config.reality.strategy != RealityStrategy.LOCAL_DECOY_SITE:
        raise DomainRotationError('rotate-domain currently supports local_decoy_site nodes only')

    new_domain = _normalize_domain(to_domain)
    if new_domain == config.domain:
        raise DomainRotationError('new domain is already the active node domain')
    old_spares = tuple(config.domain_rotation.spare_domains)
    if new_domain not in old_spares and not allow_custom_domain:
        raise DomainRotationError(
            f'{new_domain} is not in domain_rotation.spare_domains; pass --allow-custom-domain to override',
        )

    raw = config.model_dump(mode='json')
    old_domain = config.domain
    old_host_address = config.effective_host_address()
    raw['domain'] = new_domain
    raw['domain_rotation']['spare_domains'] = _next_spare_domains(old_domain, old_spares, new_domain)
    if config.host.address == old_domain:
        raw['host']['address'] = new_domain
    elif config.host.address is None:
        raw['host']['address'] = None
    else:
        raise DomainRotationError(
            f'host.address={config.host.address!r} is custom; rotate it manually or use a config where host.address is null/active domain',
        )
    raw['reality']['server_names'] = [new_domain]
    raw['site']['contact_email'] = _rotated_contact_email(config.site.contact_email, old_domain, new_domain)

    try:
        rotated = NodeConfig.model_validate(raw)
    except ValidationError as exc:
        raise DomainRotationError(f'rotated config is invalid: {exc}') from exc

    warnings = _rotation_warnings(config)
    return DomainRotationPlan(
        internal_name=config.display.internal_name,
        role=config.role.value,
        old_domain=old_domain,
        new_domain=new_domain,
        old_host_address=old_host_address,
        new_host_address=rotated.effective_host_address(),
        old_spare_domains=old_spares,
        new_spare_domains=tuple(rotated.domain_rotation.spare_domains),
        rotated_config=rotated,
        warnings=warnings,
    )


def build_sni_change_plan(
    config: NodeConfig,
    *,
    target: str,
    server_names: list[str] | tuple[str, ...] = (),
) -> SniChangePlan:
    """Build a validated remote_dest SNI change without mutating files."""

    if config.reality.strategy != RealityStrategy.REMOTE_DEST:
        raise DomainRotationError('change-sni supports remote_dest nodes only; use rotate-domain for local_decoy_site nodes')

    new_target, target_host = _normalize_reality_target(target)
    normalized_names = tuple(_normalize_domain(item) for item in server_names) if server_names else (target_host,)

    raw = config.model_dump(mode='json')
    old_target = config.reality.target
    old_server_names = tuple(config.effective_reality_server_names())
    old_xhttp_host = config.reality.xhttp.host if config.reality.xhttp else None

    raw['reality']['target'] = new_target
    raw['reality']['server_names'] = list(normalized_names)
    if raw['reality'].get('xhttp') is not None:
        raw['reality']['xhttp']['host'] = normalized_names[0]

    try:
        changed = NodeConfig.model_validate(raw)
    except ValidationError as exc:
        raise DomainRotationError(f'SNI-changed config is invalid: {exc}') from exc

    new_xhttp_host = changed.reality.xhttp.host if changed.reality.xhttp else None
    return SniChangePlan(
        internal_name=config.display.internal_name,
        role=config.role.value,
        old_target=old_target,
        new_target=new_target,
        old_server_names=old_server_names,
        new_server_names=normalized_names,
        old_xhttp_host=old_xhttp_host,
        new_xhttp_host=new_xhttp_host,
        changed_config=changed,
    )


def write_rotated_config(config: NodeConfig, path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode='json')
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return target


def write_sni_changed_config(config: NodeConfig, path: str | Path) -> Path:
    return write_rotated_config(config, path)


def rotate_reality_secret_server_names(
    secret_store: LocalSecretStore,
    ref: str,
    *,
    server_names: list[str],
) -> RealitySecretRotationResult:
    if not server_names:
        raise DomainRotationError('server_names cannot be empty')
    try:
        payload = secret_store.read_json(ref)
        path = secret_store.path_for_ref(ref)
    except SecretStoreError as exc:
        raise DomainRotationError(str(exc)) from exc
    _validate_reality_secret_payload(payload, ref)
    normalized = tuple(_normalize_domain(item) for item in server_names)
    current = tuple(str(item).strip().lower().rstrip('.') for item in payload.get('serverNames') or [])
    if current == normalized:
        return RealitySecretRotationResult(ref=ref, path=path, backup_path=None, status='existing', server_names=normalized)

    backup_path = _backup_secret_file(path, payload)
    payload['serverNames'] = list(normalized)
    try:
        secret_store.write_text(ref, json.dumps(payload, ensure_ascii=False, sort_keys=True), overwrite=True)
    except SecretStoreError as exc:
        raise DomainRotationError(str(exc)) from exc
    return RealitySecretRotationResult(ref=ref, path=path, backup_path=backup_path, status='updated', server_names=normalized)


def save_domain_rotation_state(
    state_store: NodeStateStore,
    plan: DomainRotationPlan,
    *,
    config_path: Path | None,
    old_config_path: Path | None = None,
    dns_records: list[dict[str, Any]] | None = None,
    reality_secret: RealitySecretRotationResult | None = None,
) -> Path:
    state = state_store.load_or_init(plan.rotated_config)
    rollback = _domain_rotation_rollback_state(
        plan,
        config_path=config_path,
        old_config_path=old_config_path,
        dns_records=dns_records,
        reality_secret=reality_secret,
    )
    state.pending['domain_rotation'] = {
        'status': 'prepared',
        'old_domain': plan.old_domain,
        'new_domain': plan.new_domain,
        'old_host_address': plan.old_host_address,
        'new_host_address': plan.new_host_address,
        'old_config_path': str(old_config_path) if old_config_path else None,
        'rotated_config_path': str(config_path) if config_path else None,
        'dns_records': dns_records or [],
        'reality_secret': reality_secret.to_dict() if reality_secret else None,
        'rollback': rollback,
        'prepared_at': utc_now_iso(),
    }
    return state_store.save(state)


def mark_domain_rotation_switch_started(
    state_store: NodeStateStore,
    plan: DomainRotationPlan,
    *,
    config_path: Path | None,
) -> DomainRotationSwitchStateResult:
    return _save_domain_rotation_switch_status(
        state_store,
        plan,
        config_path=config_path,
        status='switching',
    )


def mark_domain_rotation_switch_succeeded(
    state_store: NodeStateStore,
    plan: DomainRotationPlan,
    *,
    config_path: Path | None,
) -> DomainRotationSwitchStateResult:
    return _save_domain_rotation_switch_status(
        state_store,
        plan,
        config_path=config_path,
        status='switched',
    )


def mark_domain_rotation_switch_failed(
    state_store: NodeStateStore,
    plan: DomainRotationPlan,
    *,
    config_path: Path | None,
    error: str,
) -> DomainRotationSwitchStateResult:
    return _save_domain_rotation_switch_status(
        state_store,
        plan,
        config_path=config_path,
        status='switch_failed',
        error=error,
    )


def _save_domain_rotation_switch_status(
    state_store: NodeStateStore,
    plan: DomainRotationPlan,
    *,
    config_path: Path | None,
    status: Literal['switching', 'switched', 'switch_failed'],
    error: str | None = None,
) -> DomainRotationSwitchStateResult:
    state = state_store.load_or_init(plan.rotated_config)
    pending = dict(state.pending.get('domain_rotation') or {})
    rollback = dict(pending.get('rollback') or _domain_rotation_rollback_state(plan, config_path=config_path))
    timestamp_key = {
        'switching': 'switch_started_at',
        'switched': 'switched_at',
        'switch_failed': 'failed_at',
    }[status]
    pending.update(
        {
            'status': status,
            'old_domain': plan.old_domain,
            'new_domain': plan.new_domain,
            'old_host_address': plan.old_host_address,
            'new_host_address': plan.new_host_address,
            'rotated_config_path': str(config_path) if config_path else pending.get('rotated_config_path'),
            'rollback': rollback,
            timestamp_key: utc_now_iso(),
        },
    )
    if error:
        pending['error'] = error
    elif status == 'switched':
        pending.pop('error', None)
    state.pending['domain_rotation'] = pending
    if status == 'switched':
        state.update_discovered(
            {
                'active_domain': plan.new_domain,
                'previous_domain': plan.old_domain,
                'domain_rotation_status': 'switched',
                'rotated_config_path': str(config_path) if config_path else None,
            },
        )
    elif status == 'switch_failed':
        state.update_discovered({'domain_rotation_status': 'switch_failed'})
    path = state_store.save(state)
    return DomainRotationSwitchStateResult(path=path, status=status, rollback=rollback, error=error)


def _domain_rotation_rollback_state(
    plan: DomainRotationPlan,
    *,
    config_path: Path | None,
    old_config_path: Path | None = None,
    dns_records: list[dict[str, Any]] | None = None,
    reality_secret: RealitySecretRotationResult | None = None,
) -> dict[str, Any]:
    return {
        'status': 'available',
        'old_domain': plan.old_domain,
        'new_domain': plan.new_domain,
        'old_host_address': plan.old_host_address,
        'new_host_address': plan.new_host_address,
        'old_config_path': str(old_config_path) if old_config_path else None,
        'rotated_config_path': str(config_path) if config_path else None,
        'dns_records': dns_records or [],
        'reality_secret_backup_path': str(reality_secret.backup_path) if reality_secret and reality_secret.backup_path else None,
        'reality_secret_ref': reality_secret.ref if reality_secret else plan.rotated_config.reality.credentials_ref,
    }


def _normalize_domain(value: str) -> str:
    normalized = value.strip().lower().rstrip('.')
    if not DOMAIN_RE.fullmatch(normalized):
        raise DomainRotationError(f'invalid domain: {value!r}')
    return normalized


def _normalize_reality_target(value: str) -> tuple[str, str]:
    raw = value.strip().lower().rstrip('.')
    host, separator, port = raw.partition(':')
    if separator != ':' or not host or not port:
        raise DomainRotationError('REALITY target must have host:port shape, for example ya.ru:443')
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise DomainRotationError('REALITY target port must be an integer from 1 to 65535')
    normalized_host = _normalize_domain(host)
    return f'{normalized_host}:{int(port)}', normalized_host


def _next_spare_domains(old_domain: str, old_spares: tuple[str, ...], new_domain: str) -> list[str]:
    next_spares = [domain for domain in old_spares if domain != new_domain]
    if old_domain not in next_spares:
        next_spares.append(old_domain)
    return next_spares


def _rotated_contact_email(contact_email: str, old_domain: str, new_domain: str) -> str:
    local, separator, domain = contact_email.partition('@')
    if separator and domain.strip().lower().rstrip('.') == old_domain:
        return f'{local}@{new_domain}'
    return contact_email


def _rotation_warnings(config: NodeConfig) -> tuple[str, ...]:
    warnings: list[str] = []
    if config.role == NodeRole.FOREIGN_EXIT:
        warnings.append('foreign-exit rotation can require RU-edge transit config updates for transit.foreign_exit_domain/server_names')
    if config.role == NodeRole.RU_EDGE:
        warnings.append('RU-edge rotation is not an independent RKN diagnosis without a separate RU probe vantage point')
    return tuple(warnings)


def _validate_reality_secret_payload(payload: dict[str, Any], ref: str) -> None:
    required = ('privateKey', 'publicKey', 'shortIds', 'serverNames')
    missing = [key for key in required if key not in payload]
    if missing:
        raise DomainRotationError(f'REALITY secret {ref} is missing keys: {", ".join(missing)}')
    if not isinstance(payload.get('shortIds'), list) or not payload['shortIds']:
        raise DomainRotationError(f'REALITY secret {ref} shortIds must be a non-empty list')


def _backup_secret_file(path: Path, payload: dict[str, Any]) -> Path:
    suffix = utc_now_iso().replace(':', '').replace('.', '').replace('Z', 'Z')
    backup_path = path.with_name(f'{path.name}.backup-{suffix}')
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    backup_path.chmod(0o600)
    return backup_path
