"""Local end-to-end onboarding simulation for Templar nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.templar_node.fake_env import FakeEnvironmentError, FakeEnvironmentStore, ensure_record, fake_uuid
from app.templar_node.render import render_bundle, write_bundle
from app.templar_node.schemas import NodeConfig, TransitMode
from app.templar_node.state import LAYER1_STEPS, NodeState, NodeStateStore, utc_now_iso


class SimulationError(RuntimeError):
    """Raised when a fake onboarding run finds an inconsistent state."""


@dataclass(frozen=True)
class SimulationAction:
    layer: str
    resource: str
    key: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            'layer': self.layer,
            'resource': self.resource,
            'key': self.key,
            'status': self.status,
            'details': self.details,
        }

    def to_line(self) -> str:
        detail_bits = [f'{key}={value}' for key, value in sorted(self.details.items())]
        details = f' ({", ".join(detail_bits)})' if detail_bits else ''
        return f'- [{self.layer}] {self.status} {self.resource}: {self.key}{details}'


@dataclass
class SimulationResult:
    internal_name: str
    role: str
    env_path: Path
    state_path: Path
    actions: list[SimulationAction]
    render_dir: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'internal_name': self.internal_name,
            'role': self.role,
            'env_path': str(self.env_path),
            'state_path': str(self.state_path),
            'render_dir': str(self.render_dir) if self.render_dir else None,
            'actions': [action.to_dict() for action in self.actions],
        }

    def to_lines(self) -> list[str]:
        lines = [
            f'Simulation: {self.internal_name} ({self.role})',
            f'Environment: {self.env_path}',
            f'State: {self.state_path}',
        ]
        if self.render_dir is not None:
            lines.append(f'Rendered artifacts: {self.render_dir}')
        lines.append('Actions:')
        lines.extend(action.to_line() for action in self.actions)
        return lines


def simulate_onboarding(
    config: NodeConfig,
    *,
    env_store: FakeEnvironmentStore,
    state_store: NodeStateStore,
    render_dir: Path | None = None,
) -> SimulationResult:
    try:
        environment = env_store.load()
    except FakeEnvironmentError as exc:
        raise SimulationError(str(exc)) from exc
    state = state_store.load_or_init(config)
    actions: list[SimulationAction] = []

    _simulate_layer2a(config, environment, state, actions)
    _simulate_layer1(config, environment, state, actions, render_dir)
    _simulate_layer2b(config, environment, state, actions)

    try:
        env_path = env_store.save(environment)
    except FakeEnvironmentError as exc:
        raise SimulationError(str(exc)) from exc
    state_path = state_store.save(state)
    return SimulationResult(
        internal_name=config.display.internal_name,
        role=config.role.value,
        env_path=env_path,
        state_path=state_path,
        actions=actions,
        render_dir=render_dir / config.display.internal_name if render_dir else None,
    )


def _simulate_layer2a(
    config: NodeConfig,
    environment: dict[str, Any],
    state: NodeState,
    actions: list[SimulationAction],
) -> None:
    profile_uuid = (
        config.xray.config_profile_uuid
        or state.discovered.get('config_profile_uuid')
        or fake_uuid('config-profile', f'{config.role.value}:{config.display.internal_name}')
    )
    profile_record = {
        'uuid': profile_uuid,
        'name': f'{config.role.value}:{config.display.internal_name}',
        'role': config.role.value,
    }
    status = ensure_record(environment['remnawave']['config_profiles'], profile_uuid, profile_record)
    actions.append(SimulationAction('Layer 2a', 'config_profile', profile_uuid, status))

    node_uuid = fake_uuid('remnawave-node', config.display.internal_name)
    node_record = {
        'uuid': node_uuid,
        'internal_name': config.display.internal_name,
        'role': config.role.value,
        'country_code': config.country_code,
        'domain': config.domain,
        'public_ipv4': config.public_ipv4,
        'node_port': config.remnanode.node_port,
        'secret_key_ref': config.remnanode.secret_key_ref,
    }
    status = ensure_record(environment['remnawave']['nodes'], config.display.internal_name, node_record)
    actions.append(SimulationAction('Layer 2a', 'remnawave_node', config.display.internal_name, status))

    state.update_discovered({'config_profile_uuid': profile_uuid, 'remnawave_node_uuid': node_uuid})
    state.mark_checkpoint('remnawave_node_registered')


def _simulate_layer1(
    config: NodeConfig,
    environment: dict[str, Any],
    state: NodeState,
    actions: list[SimulationAction],
    render_dir: Path | None,
) -> None:
    if render_dir is not None:
        output_dir = render_dir / config.display.internal_name
        written = write_bundle(render_bundle(config), output_dir)
        actions.append(
            SimulationAction(
                'Layer 1',
                'render_bundle',
                str(output_dir),
                'written',
                {'files': len(written)},
            ),
        )

    ssh_run = {
        'node': config.display.internal_name,
        'admin_user': config.ssh.admin_user,
        'ssh_port': config.ssh.port,
        'public_ipv4': config.public_ipv4,
        'simulated_at': utc_now_iso(),
    }
    environment['ssh']['runs'].append(ssh_run)
    actions.append(SimulationAction('Layer 1', 'ssh_bootstrap', config.public_ipv4, 'simulated'))

    node = environment['remnawave']['nodes'][config.display.internal_name]
    node['online'] = True
    node['online_checked_at'] = utc_now_iso()
    actions.append(SimulationAction('Layer 1', 'remnawave_node_status', config.display.internal_name, 'online'))

    for checkpoint in LAYER1_STEPS:
        state.mark_checkpoint(checkpoint)


def _simulate_layer2b(
    config: NodeConfig,
    environment: dict[str, Any],
    state: NodeState,
    actions: list[SimulationAction],
) -> None:
    state.mark_checkpoint('node_online')

    host_uuid = fake_uuid('host', config.display.internal_name)
    host_record = {
        'uuid': host_uuid,
        'node': config.display.internal_name,
        'address': config.effective_host_address(),
        'port': config.host.port,
        'remark': config.effective_host_remark(),
        'display_name': config.effective_cabinet_name(),
        'visibility': config.host.visibility,
        'inbound_ref': config.host.inbound_ref,
    }
    status = ensure_record(environment['remnawave']['hosts'], config.display.internal_name, host_record)
    actions.append(SimulationAction('Layer 2b', 'host', config.display.internal_name, status, {'uuid': host_uuid}))
    state.mark_checkpoint('host_ok')

    internal_squad_uuid = fake_uuid('internal-squad', config.bedolaga.internal_squad_name)
    internal_squad_record = {
        'uuid': internal_squad_uuid,
        'name': config.bedolaga.internal_squad_name,
        'host_uuid': host_uuid,
        'node': config.display.internal_name,
    }
    status = ensure_record(
        environment['remnawave']['internal_squads'],
        config.bedolaga.internal_squad_name,
        internal_squad_record,
    )
    actions.append(
        SimulationAction(
            'Layer 2b',
            'internal_squad',
            config.bedolaga.internal_squad_name,
            status,
            {'uuid': internal_squad_uuid},
        ),
    )
    state.mark_checkpoint('internal_squad_ok')

    external_squad_uuid = fake_uuid('external-squad', config.bedolaga.external_squad_name)
    external_squad_record = {
        'uuid': external_squad_uuid,
        'name': config.bedolaga.external_squad_name,
        'internal_squad_uuids': [internal_squad_uuid],
    }
    status = ensure_record(
        environment['remnawave']['external_squads'],
        config.bedolaga.external_squad_name,
        external_squad_record,
    )
    actions.append(
        SimulationAction(
            'Layer 2b',
            'external_squad',
            config.bedolaga.external_squad_name,
            status,
            {'uuid': external_squad_uuid},
        ),
    )
    state.mark_checkpoint('external_squad_ok')

    if config.transit.mode != TransitMode.DISABLED and config.transit.service_user:
        _simulate_service_user(config, environment, actions)
        state.mark_checkpoint('transit_user_ok')

    profile_uuid = str(state.discovered['config_profile_uuid'])
    profile_update_key = f'{profile_uuid}:{config.display.internal_name}'
    profile_update_record = {
        'profile_uuid': profile_uuid,
        'node': config.display.internal_name,
        'role': config.role.value,
        'public_inbound_uuid': config.xray.public_inbound_uuid,
        'transit_mode': config.transit.mode.value,
        'warp_mode': config.warp.mode.value,
    }
    status = ensure_record(environment['remnawave']['profile_updates'], profile_update_key, profile_update_record)
    actions.append(SimulationAction('Layer 2b', 'profile_update', profile_update_key, status))
    state.mark_checkpoint('remnawave_config_ok')

    state.mark_checkpoint('bedolaga_pending')
    _simulate_server_squad_attach(config, environment, actions, internal_squad_uuid)
    _simulate_tariff_attach(config, environment, actions, internal_squad_uuid, external_squad_uuid)
    state.mark_checkpoint('bedolaga_ok')
    state.mark_checkpoint('subscriptions_resynced')
    state.update_discovered(
        {
            'host_uuid': host_uuid,
            'internal_squad_uuid': internal_squad_uuid,
            'external_squad_uuid': external_squad_uuid,
        },
    )


def _simulate_service_user(
    config: NodeConfig,
    environment: dict[str, Any],
    actions: list[SimulationAction],
) -> None:
    service_user = config.transit.service_user
    if service_user is None:
        return
    key = f'system:transit:{service_user}'
    existing = environment['remnawave']['service_users'].get(key)
    credential_ref = config.transit.service_user_credential_ref
    if existing is not None and existing.get('credential_ref') != credential_ref:
        raise SimulationError(
            f'service user {service_user!r} already exists with a different credential ref; '
            'manual cleanup is required',
        )
    record = {
        'uuid': fake_uuid('service-user', key),
        'username': service_user,
        'tag': 'system:transit',
        'credential_ref': credential_ref,
        'status': 'ACTIVE',
        'expire_at': None,
        'traffic_limit_bytes': 0,
    }
    status = ensure_record(environment['remnawave']['service_users'], key, record)
    actions.append(SimulationAction('Layer 2b', 'service_user', service_user, status, {'tag': 'system:transit'}))


def _simulate_server_squad_attach(
    config: NodeConfig,
    environment: dict[str, Any],
    actions: list[SimulationAction],
    internal_squad_uuid: str,
) -> None:
    server_squads = environment['bedolaga'].setdefault('server_squads', {})
    record = {
        'squad_uuid': internal_squad_uuid,
        'display_name': config.effective_cabinet_name(),
        'original_name': config.bedolaga.internal_squad_name,
        'country_code': config.country_code,
        'is_available': config.host.visibility,
        'is_trial_eligible': config.bedolaga.trial_eligible,
    }
    status = ensure_record(server_squads, internal_squad_uuid, record)
    actions.append(SimulationAction('Layer 2b', 'server_squad', internal_squad_uuid, status))


def _simulate_tariff_attach(
    config: NodeConfig,
    environment: dict[str, Any],
    actions: list[SimulationAction],
    internal_squad_uuid: str,
    external_squad_uuid: str,
) -> None:
    tariff_refs = [
        *(('slug', value) for value in config.bedolaga.attach_to_tariff_slugs),
        *(('name', value) for value in config.bedolaga.attach_to_tariff_names),
    ]
    for ref_type, ref_value in tariff_refs:
        key = f'{ref_type}:{ref_value}'
        tariff = environment['bedolaga']['tariffs'].setdefault(
            key,
            {
                'ref_type': ref_type,
                'ref_value': ref_value,
                'allowed_internal_squad_uuids': [],
                'external_squad_uuids': [],
            },
        )
        changed = False
        if internal_squad_uuid not in tariff['allowed_internal_squad_uuids']:
            tariff['allowed_internal_squad_uuids'].append(internal_squad_uuid)
            changed = True
        if external_squad_uuid not in tariff['external_squad_uuids']:
            tariff['external_squad_uuids'].append(external_squad_uuid)
            changed = True
        status = 'attached' if changed else 'existing'
        actions.append(SimulationAction('Layer 2b', 'tariff', key, status))
        if changed:
            _append_unique_resync(environment, key, config.display.internal_name)

    if config.bedolaga.trial_eligible:
        _append_unique_resync(environment, 'trial:eligible', config.display.internal_name)


def _append_unique_resync(environment: dict[str, Any], tariff_key: str, internal_name: str) -> None:
    resync_key = f'{tariff_key}:{internal_name}'
    if any(item.get('key') == resync_key for item in environment['bedolaga']['resyncs']):
        return
    environment['bedolaga']['resyncs'].append(
        {
            'key': resync_key,
            'tariff': tariff_key,
            'node': internal_name,
            'status': 'subscriptions_resynced',
        },
    )
