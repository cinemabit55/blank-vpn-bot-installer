"""Shared fake control-plane environment helpers."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.templar_node.state import utc_now_iso


FAKE_ENV_SCHEMA_VERSION = 1


class FakeEnvironmentError(RuntimeError):
    """Raised when the local fake environment cannot be read or written."""


class FakeEnvironmentStore:
    """Store a fake RemnaWave/Bedolaga/control-plane environment as JSON."""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.path = self.root_dir / 'fake-environment.json'

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return new_environment()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise FakeEnvironmentError(f'cannot read fake environment {self.path}: {exc}') from exc
        if not isinstance(raw, dict):
            raise FakeEnvironmentError(f'fake environment {self.path} must contain a JSON object')
        if raw.get('schema_version') != FAKE_ENV_SCHEMA_VERSION:
            raise FakeEnvironmentError(f'unsupported fake environment schema_version: {raw.get("schema_version")!r}')
        return raw

    def save(self, environment: dict[str, Any]) -> Path:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        environment['updated_at'] = utc_now_iso()
        tmp_path = self.root_dir / f'.fake-environment.{uuid.uuid4().hex}.tmp'
        data = json.dumps(environment, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            tmp_path.write_text(f'{data}\n', encoding='utf-8')
            tmp_path.chmod(0o600)
            tmp_path.replace(self.path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise FakeEnvironmentError(f'cannot write fake environment {self.path}: {exc}') from exc
        return self.path


def ensure_record(mapping: dict[str, Any], key: str, desired: dict[str, Any]) -> str:
    existing = mapping.get(key)
    if existing is None:
        mapping[key] = dict(desired)
        return 'created'
    merged = {**existing, **desired}
    if merged != existing:
        mapping[key] = merged
        return 'updated'
    return 'existing'


def fake_uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'templar-node:{kind}:{key}'))


def new_environment() -> dict[str, Any]:
    timestamp = utc_now_iso()
    return {
        'schema_version': FAKE_ENV_SCHEMA_VERSION,
        'created_at': timestamp,
        'updated_at': timestamp,
        'remnawave': {
            'config_profiles': {},
            'nodes': {},
            'hosts': {},
            'internal_squads': {},
            'external_squads': {},
            'service_users': {},
            'profile_updates': {},
        },
        'bedolaga': {
            'tariffs': {},
            'resyncs': [],
        },
        'ssh': {
            'runs': [],
        },
        'dns': {
            'records': {},
        },
    }
