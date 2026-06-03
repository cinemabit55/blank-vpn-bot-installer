"""Checkpoint/state storage for Templar node onboarding."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from app.templar_node.schemas import NodeConfig


STATE_SCHEMA_VERSION = 1
SCRIPT_VERSION = '0.1.0'

LAYER1_STEPS = (
    'preflight_ok',
    'os_detected',
    'packages_installed',
    'docker_installed',
    'admin_user_created',
    'ssh_hardened',
    'ufw_applied',
    'remnanode_dirs_created',
    'site_written',
    'caddy_installed',
    'certs_installed',
    'remnanode_compose_written',
    'containers_running',
    'health_ok',
)
LAYER2A_CHECKPOINTS = ('remnawave_node_registered',)
LAYER2B_CHECKPOINTS = (
    'node_online',
    'host_ok',
    'internal_squad_ok',
    'external_squad_ok',
    'transit_user_ok',
    'remnawave_config_ok',
    'bedolaga_pending',
    'bedolaga_ok',
    'subscriptions_resynced',
)
KNOWN_CHECKPOINTS = frozenset((*LAYER1_STEPS, *LAYER2A_CHECKPOINTS, *LAYER2B_CHECKPOINTS))


class StateStoreError(ValueError):
    """Raised when onboarding state cannot be read or written safely."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


@dataclass
class NodeState:
    schema_version: int
    script_version: str
    run_id: str
    internal_name: str
    role: str
    last_completed_step: str | None
    checkpoints: dict[str, str] = field(default_factory=dict)
    discovered: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)
    orphaned: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def initial(cls, config: NodeConfig) -> NodeState:
        return cls(
            schema_version=STATE_SCHEMA_VERSION,
            script_version=SCRIPT_VERSION,
            run_id=str(uuid.uuid4()),
            internal_name=config.display.internal_name,
            role=config.role.value,
            last_completed_step=None,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NodeState:
        schema_version = raw.get('schema_version')
        if schema_version != STATE_SCHEMA_VERSION:
            raise StateStoreError(f'unsupported state schema_version: {schema_version!r}')
        return cls(
            schema_version=schema_version,
            script_version=str(raw.get('script_version', '')),
            run_id=str(raw.get('run_id', '')),
            internal_name=str(raw.get('internal_name', '')),
            role=str(raw.get('role', '')),
            last_completed_step=raw.get('last_completed_step'),
            checkpoints=dict(raw.get('checkpoints') or {}),
            discovered=dict(raw.get('discovered') or {}),
            pending=dict(raw.get('pending') or {}),
            orphaned=list(raw.get('orphaned') or []),
            updated_at=str(raw.get('updated_at') or utc_now_iso()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'script_version': self.script_version,
            'run_id': self.run_id,
            'internal_name': self.internal_name,
            'role': self.role,
            'last_completed_step': self.last_completed_step,
            'checkpoints': self.checkpoints,
            'discovered': self.discovered,
            'pending': self.pending,
            'orphaned': self.orphaned,
            'updated_at': self.updated_at,
        }

    def mark_checkpoint(self, checkpoint: str) -> None:
        validate_checkpoint(checkpoint)
        timestamp = utc_now_iso()
        self.last_completed_step = checkpoint
        self.checkpoints[checkpoint] = timestamp
        self.updated_at = timestamp

    def update_discovered(self, values: dict[str, Any]) -> None:
        self.discovered.update(values)
        self.updated_at = utc_now_iso()

    def clear_orphaned(self) -> int:
        count = len(self.orphaned)
        self.orphaned = []
        self.updated_at = utc_now_iso()
        return count


class NodeStateStore:
    """Store node onboarding state below a control-plane state root."""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).expanduser().resolve()

    def node_dir(self, internal_name: str) -> Path:
        safe_name = _safe_node_name(internal_name)
        return self.root_dir / safe_name

    def state_path(self, internal_name: str) -> Path:
        return self.node_dir(internal_name) / 'state.json'

    def lock_path(self) -> Path:
        return self.root_dir / '.templar-onboarding.lock'

    def control_plane_lock(self, label: str) -> StateFileLock:
        return StateFileLock(self.lock_path(), label=label)

    def load(self, internal_name: str) -> NodeState | None:
        path = self.state_path(internal_name)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(f'cannot read state {path}: {exc}') from exc
        if not isinstance(raw, dict):
            raise StateStoreError(f'state {path} must contain a JSON object')
        return NodeState.from_dict(raw)

    def load_or_init(self, config: NodeConfig) -> NodeState:
        state = self.load(config.display.internal_name)
        if state is not None:
            return state
        return NodeState.initial(config)

    def save(self, state: NodeState) -> Path:
        node_dir = self.node_dir(state.internal_name)
        node_dir.mkdir(parents=True, exist_ok=True)
        path = node_dir / 'state.json'
        tmp_path = node_dir / f'.state.{uuid.uuid4().hex}.tmp'
        data = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        try:
            tmp_path.write_text(f'{data}\n', encoding='utf-8')
            tmp_path.chmod(0o600)
            tmp_path.replace(path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateStoreError(f'cannot write state {path}: {exc}') from exc
        return path

    def mark_checkpoint(
        self,
        config: NodeConfig,
        checkpoint: str,
        *,
        discovered: dict[str, Any] | None = None,
    ) -> NodeState:
        state = self.load_or_init(config)
        state.mark_checkpoint(checkpoint)
        if discovered:
            state.update_discovered(discovered)
        self.save(state)
        return state

    def iter_states(self) -> list[tuple[Path, NodeState]]:
        if not self.root_dir.exists():
            return []
        states: list[tuple[Path, NodeState]] = []
        for path in sorted(self.root_dir.glob('*/state.json')):
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise StateStoreError(f'cannot read state {path}: {exc}') from exc
            if not isinstance(raw, dict):
                raise StateStoreError(f'state {path} must contain a JSON object')
            states.append((path, NodeState.from_dict(raw)))
        return states


class StateFileLock:
    """Non-blocking filesystem lock for control-plane mutations."""

    def __init__(self, path: Path, *, label: str):
        self.path = path
        self.label = label
        self._file = None

    def __enter__(self) -> Self:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - fcntl is present on Linux/macOS.
            raise StateStoreError('filesystem control-plane lock requires fcntl') from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open('a+', encoding='utf-8')
        try:
            self.path.chmod(0o600)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock_file.seek(0)
                owner = lock_file.read().strip() or 'unknown owner'
                raise StateStoreError(f'control-plane lock busy at {self.path}: {owner}') from exc
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(json.dumps({'pid': os.getpid(), 'label': self.label, 'locked_at': utc_now_iso()}, sort_keys=True))
            lock_file.write('\n')
            lock_file.flush()
        except Exception:
            lock_file.close()
            raise
        self._file = lock_file
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is None:
            return
        try:
            import fcntl

            self._file.seek(0)
            self._file.truncate()
            self._file.flush()
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def validate_checkpoint(checkpoint: str) -> None:
    if checkpoint not in KNOWN_CHECKPOINTS:
        known = ', '.join(sorted(KNOWN_CHECKPOINTS))
        raise StateStoreError(f'unknown checkpoint {checkpoint!r}; known checkpoints: {known}')


def _safe_node_name(internal_name: str) -> str:
    if not internal_name or any(char in internal_name for char in ('/', '\\', '\0')):
        raise StateStoreError(f'unsafe node internal_name: {internal_name!r}')
    if internal_name in {'.', '..'}:
        raise StateStoreError(f'unsafe node internal_name: {internal_name!r}')
    return internal_name
