"""Read-only RemnaWave panel contract checks for onboarding."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.templar_node.http_json import JsonHttpClient, JsonHttpError


class RemnaWaveProbeError(RuntimeError):
    """Raised when the RemnaWave read-only probe cannot complete."""


@dataclass(frozen=True)
class RemnaWaveProbeAuth:
    api_key: str
    auth_type: str = 'api_key'
    caddy_token: str | None = None

    def headers(self) -> dict[str, str]:
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'templar-node/0.1',
        }
        auth_type = self.auth_type.lower().strip()
        if auth_type == 'caddy':
            headers['Authorization'] = f'Bearer {self.api_key}'
            if self.caddy_token:
                headers['X-Api-Key'] = self.caddy_token
        elif auth_type == 'bearer':
            headers['Authorization'] = f'Bearer {self.api_key}'
        else:
            headers['X-Api-Key'] = self.api_key
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers


@dataclass(frozen=True)
class RemnaWaveProbeResult:
    api_url: str
    metadata: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    nodes_total: int = 0
    nodes_online: int = 0
    nodes_disabled: int = 0
    internal_squads_total: int = 0
    external_squads_total: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            'api_url': self.api_url,
            'metadata': self.metadata,
            'stats': self.stats,
            'nodes_total': self.nodes_total,
            'nodes_online': self.nodes_online,
            'nodes_disabled': self.nodes_disabled,
            'internal_squads_total': self.internal_squads_total,
            'external_squads_total': self.external_squads_total,
            'warnings': list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_lines(self) -> list[str]:
        version = self.metadata.get('version') or '<unknown>'
        build = self.metadata.get('build')
        build_label = ''
        if isinstance(build, dict) and build.get('number'):
            build_label = f" build={build['number']}"
        lines = [
            f'RemnaWave API: {self.api_url}',
            f'Version: {version}{build_label}',
            f'Nodes: total={self.nodes_total} online={self.nodes_online} disabled={self.nodes_disabled}',
            f'Internal squads: {self.internal_squads_total}',
            f'External squads: {self.external_squads_total}',
        ]
        lines.extend(f'WARNING: {warning}' for warning in self.warnings)
        return lines


def run_remnawave_probe(
    *,
    api_url: str,
    auth: RemnaWaveProbeAuth,
    timeout_seconds: int = 20,
    verify_tls: bool = True,
) -> RemnaWaveProbeResult:
    client = JsonHttpClient(
        base_url=api_url,
        headers=auth.headers(),
        timeout_seconds=timeout_seconds,
        verify_tls=verify_tls,
    )
    try:
        metadata = _response_object(client.get('/api/system/metadata'))
        stats = _response_object(client.get('/api/system/stats'))
        nodes = _response_list(client.get('/api/nodes'))
        internal_squads = _response_keyed_list(client.get('/api/internal-squads'), 'internalSquads')
        external_squads = _response_keyed_list(client.get('/api/external-squads'), 'externalSquads')
    except JsonHttpError as exc:
        raise RemnaWaveProbeError(f'RemnaWave check failed: {exc}') from exc

    warnings = _metadata_warnings(metadata)
    return RemnaWaveProbeResult(
        api_url=api_url.rstrip('/'),
        metadata=metadata,
        stats=stats,
        nodes_total=len(nodes),
        nodes_online=sum(1 for node in nodes if bool(node.get('isConnected')) and not bool(node.get('isDisabled'))),
        nodes_disabled=sum(1 for node in nodes if bool(node.get('isDisabled'))),
        internal_squads_total=len(internal_squads),
        external_squads_total=len(external_squads),
        warnings=warnings,
    )


def fetch_remnawave_node_secret_key(
    *,
    api_url: str,
    auth: RemnaWaveProbeAuth,
    timeout_seconds: int = 20,
    verify_tls: bool = True,
) -> str:
    """Fetch a RemnaWave Node SECRET_KEY via the official keygen endpoint."""
    client = JsonHttpClient(
        base_url=api_url,
        headers=auth.headers(),
        timeout_seconds=timeout_seconds,
        verify_tls=verify_tls,
    )
    try:
        payload = client.get('/api/keygen')
    except JsonHttpError as exc:
        raise RemnaWaveProbeError(f'RemnaWave keygen failed: {exc}') from exc
    response = payload.get('response')
    if not isinstance(response, dict):
        raise RemnaWaveProbeError('RemnaWave keygen response has no response object')
    value = str(response.get('pubKey') or '').strip()
    if not value:
        raise RemnaWaveProbeError('RemnaWave keygen response has no pubKey')
    return value


def _response_object(payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get('response')
    return response if isinstance(response, dict) else {}


def _response_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get('response')
    return [item for item in response if isinstance(item, dict)] if isinstance(response, list) else []


def _response_keyed_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    response = payload.get('response')
    if not isinstance(response, dict):
        return []
    values = response.get(key)
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _metadata_warnings(metadata: dict[str, Any]) -> tuple[str, ...]:
    version = str(metadata.get('version') or '')
    if not version:
        return ('system metadata did not include version',)
    if not version.startswith('2.7.'):
        return (f'expected RemnaWave Panel 2.7.x per architecture doc, got {version}',)
    return ()
