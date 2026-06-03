"""Layer 2b post-bootstrap orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from app.templar_node.bedolaga import BedolagaAdapter, BedolagaAdapterError, TariffAttachRecord
from app.templar_node.remnawave import RemnaWaveAdapterError, RemnaWaveControlPlaneAdapter
from app.templar_node.schemas import NodeConfig
from app.templar_node.state import NodeStateStore, StateStoreError


class Layer2bError(RuntimeError):
    """Raised when Layer 2b cannot finish safely."""


ProgressReporter = Callable[[str], None]


@dataclass(frozen=True)
class Layer2bResult:
    internal_name: str
    host_uuid: str
    host_status: str
    internal_squad_uuid: str
    internal_squad_status: str
    external_squad_uuid: str
    external_squad_status: str
    service_user_uuid: str | None
    service_user_status: str | None
    profile_update_key: str
    profile_update_status: str
    tariff_records: tuple[TariffAttachRecord, ...]
    resync_keys: tuple[str, ...]
    state_path: Path
    last_completed_step: str

    def to_lines(self) -> list[str]:
        lines = [
            f'Layer 2b post-bootstrap: {self.internal_name}',
            f'Host: {self.host_uuid} ({self.host_status})',
            f'Internal squad: {self.internal_squad_uuid} ({self.internal_squad_status})',
            f'External squad: {self.external_squad_uuid} ({self.external_squad_status})',
        ]
        if self.service_user_uuid:
            lines.append(f'Transit service user: {self.service_user_uuid} ({self.service_user_status})')
        else:
            lines.append('Transit service user: skipped')
        lines.append(f'Profile update: {self.profile_update_key} ({self.profile_update_status})')
        lines.append(f'Tariffs touched: {len(self.tariff_records)}')
        lines.extend(f'- {record.key}: {record.status}' for record in self.tariff_records)
        lines.append(f'Resyncs queued: {len(self.resync_keys)}')
        lines.extend(f'- {key}' for key in self.resync_keys)
        lines.append(f'State: {self.state_path}')
        lines.append(f'Last checkpoint: {self.last_completed_step}')
        return lines


def _progress(progress: ProgressReporter | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _ensure_node_online_with_retry(
    config: NodeConfig,
    *,
    remnawave_adapter: RemnaWaveControlPlaneAdapter,
    progress: ProgressReporter | None,
    timeout_seconds: int,
    interval_seconds: int,
) -> None:
    deadline = time.monotonic() + max(0, timeout_seconds)
    attempt = 0
    while True:
        try:
            remnawave_adapter.ensure_node_online(config)
            return
        except RemnaWaveAdapterError as exc:
            message = str(exc)
            if 'not online' not in message or time.monotonic() >= deadline:
                raise
            attempt += 1
            sleep_for = min(max(1, interval_seconds), max(1, int(deadline - time.monotonic())))
            _progress(
                progress,
                f'{config.display.internal_name}: RemnaWave node is not online yet; waiting {sleep_for}s before retry {attempt}',
            )
            time.sleep(sleep_for)


def run_layer2b_post_bootstrap(
    config: NodeConfig,
    *,
    remnawave_adapter: RemnaWaveControlPlaneAdapter,
    bedolaga_adapter: BedolagaAdapter,
    state_store: NodeStateStore,
    progress: ProgressReporter | None = None,
    node_online_timeout_seconds: int = 0,
    node_online_interval_seconds: int = 10,
) -> Layer2bResult:
    try:
        with state_store.control_plane_lock(f'layer2b:{config.display.internal_name}'):
            _progress(progress, f'{config.display.internal_name}: loading Layer 2b state')
            state = state_store.load_or_init(config)
            profile_uuid = state.discovered.get('config_profile_uuid') or config.xray.config_profile_uuid
            if not profile_uuid:
                raise Layer2bError('missing config_profile_uuid; run pre-bootstrap first')

            _progress(progress, f'{config.display.internal_name}: checking RemnaWave node online')
            _ensure_node_online_with_retry(
                config,
                remnawave_adapter=remnawave_adapter,
                progress=progress,
                timeout_seconds=node_online_timeout_seconds,
                interval_seconds=node_online_interval_seconds,
            )
            state.mark_checkpoint('node_online')

            _progress(progress, f'{config.display.internal_name}: ensuring RemnaWave Host')
            host = remnawave_adapter.ensure_host(config)
            state.mark_checkpoint('host_ok')

            _progress(progress, f'{config.display.internal_name}: ensuring internal squad')
            internal_squad = remnawave_adapter.ensure_internal_squad(config, host_uuid=host.uuid)
            state.mark_checkpoint('internal_squad_ok')

            _progress(progress, f'{config.display.internal_name}: ensuring external squad')
            external_squad = remnawave_adapter.ensure_external_squad(config, internal_squad_uuid=internal_squad.uuid)
            state.mark_checkpoint('external_squad_ok')

            _progress(progress, f'{config.display.internal_name}: ensuring transit service user')
            service_user = remnawave_adapter.ensure_transit_service_user(
                config,
                internal_squad_uuid=internal_squad.uuid,
            )
            if service_user is not None:
                state.mark_checkpoint('transit_user_ok')

            _progress(progress, f'{config.display.internal_name}: updating RemnaWave config profile')
            profile_update = remnawave_adapter.ensure_profile_update(config, profile_uuid=str(profile_uuid))
            state.mark_checkpoint('remnawave_config_ok')

            state.mark_checkpoint('bedolaga_pending')
            _progress(progress, f'{config.display.internal_name}: attaching squads to Bedolaga tariffs and resyncing subscriptions')
            bedolaga = bedolaga_adapter.attach_squads(
                config,
                internal_squad_uuid=internal_squad.uuid,
                external_squad_uuid=external_squad.uuid,
            )
            state.mark_checkpoint('bedolaga_ok')
            state.mark_checkpoint('subscriptions_resynced')

            state.update_discovered(
                {
                    'host_uuid': host.uuid,
                    'internal_squad_uuid': internal_squad.uuid,
                    'external_squad_uuid': external_squad.uuid,
                    'profile_update_key': profile_update.key,
                },
            )
            if service_user is not None:
                state.update_discovered({'transit_service_user_uuid': service_user.uuid})
            _progress(progress, f'{config.display.internal_name}: saving Layer 2b state')
            state_path = state_store.save(state)
    except (RemnaWaveAdapterError, BedolagaAdapterError, StateStoreError) as exc:
        raise Layer2bError(str(exc)) from exc
    return Layer2bResult(
        internal_name=config.display.internal_name,
        host_uuid=host.uuid,
        host_status=host.status,
        internal_squad_uuid=internal_squad.uuid,
        internal_squad_status=internal_squad.status,
        external_squad_uuid=external_squad.uuid,
        external_squad_status=external_squad.status,
        service_user_uuid=service_user.uuid if service_user else None,
        service_user_status=service_user.status if service_user else None,
        profile_update_key=profile_update.key,
        profile_update_status=profile_update.status,
        tariff_records=bedolaga.tariff_records,
        resync_keys=bedolaga.resync_keys,
        state_path=state_path,
        last_completed_step=state.last_completed_step or '',
    )
