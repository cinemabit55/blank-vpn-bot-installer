"""Layer 2a pre-bootstrap orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.templar_node.remnawave import LocalRemnaWaveAdapter, RemnaWaveAdapterError, RemnaWaveControlPlaneAdapter
from app.templar_node.schemas import NodeConfig
from app.templar_node.secrets import LocalSecretStore, SecretStoreError
from app.templar_node.state import NodeStateStore, StateStoreError


class Layer2aError(RuntimeError):
    """Raised when Layer 2a cannot finish safely."""


ProgressReporter = Callable[[str], None]


@dataclass(frozen=True)
class Layer2aResult:
    internal_name: str
    config_profile_uuid: str
    config_profile_status: str
    remnawave_node_uuid: str
    remnawave_node_status: str
    secret_ref: str
    secret_path: Path
    state_path: Path

    def to_lines(self) -> list[str]:
        return [
            f'Layer 2a pre-bootstrap: {self.internal_name}',
            f'Config profile: {self.config_profile_uuid} ({self.config_profile_status})',
            f'RemnaWave Node: {self.remnawave_node_uuid} ({self.remnawave_node_status})',
            f'SECRET_KEY ref: {self.secret_ref}',
            f'SECRET_KEY path: {self.secret_path}',
            f'State: {self.state_path}',
        ]


def _progress(progress: ProgressReporter | None, message: str) -> None:
    if progress is not None:
        progress(message)


def run_layer2a_pre_bootstrap(
    config: NodeConfig,
    *,
    adapter: RemnaWaveControlPlaneAdapter,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    progress: ProgressReporter | None = None,
) -> Layer2aResult:
    try:
        with state_store.control_plane_lock(f'layer2a:{config.display.internal_name}'):
            _progress(progress, f'{config.display.internal_name}: loading Layer 2a state')
            state = state_store.load_or_init(config)
            if isinstance(adapter, LocalRemnaWaveAdapter) and adapter.secret_store is None:
                adapter.secret_store = secret_store
            _progress(progress, f'{config.display.internal_name}: ensuring RemnaWave config profile')
            profile = adapter.ensure_config_profile(
                config,
                discovered_uuid=state.discovered.get('config_profile_uuid'),
            )
            _progress(progress, f'{config.display.internal_name}: ensuring RemnaWave node and SECRET_KEY')
            node = adapter.ensure_node(config)
            secret_path = secret_store.write_text(config.remnanode.secret_key_ref, node.secret_key, overwrite=True)
            state.update_discovered(
                {
                    'config_profile_uuid': profile.uuid,
                    'remnawave_node_uuid': node.uuid,
                    'remnanode_secret_key_ref': config.remnanode.secret_key_ref,
                },
            )
            state.mark_checkpoint('remnawave_node_registered')
            _progress(progress, f'{config.display.internal_name}: saving Layer 2a state')
            state_path = state_store.save(state)
    except (RemnaWaveAdapterError, SecretStoreError, StateStoreError) as exc:
        raise Layer2aError(str(exc)) from exc
    return Layer2aResult(
        internal_name=config.display.internal_name,
        config_profile_uuid=profile.uuid,
        config_profile_status=profile.status,
        remnawave_node_uuid=node.uuid,
        remnawave_node_status=node.status,
        secret_ref=config.remnanode.secret_key_ref,
        secret_path=secret_path,
        state_path=state_path,
    )
