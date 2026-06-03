"""Local route override storage for RU cascade nodes."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from typing import Any

import yaml

from app.templar_node.schemas import DOMAIN_RE, NodeConfig, NodeRole
from app.templar_node.state import utc_now_iso


ROUTES_SCHEMA_VERSION = 1


class RouteOverrideError(ValueError):
    """Raised when a route override cannot be added safely."""


@dataclass(frozen=True)
class RouteAddResult:
    path: Path
    node: str
    added_domains: tuple[str, ...]
    added_ips: tuple[str, ...]
    existing_domains: tuple[str, ...]
    existing_ips: tuple[str, ...]

    def to_lines(self) -> list[str]:
        lines = [
            f'Route overrides: {self.path}',
            f'Node: {self.node}',
        ]
        lines.append(f'Added domains: {len(self.added_domains)}')
        lines.extend(f'- {item}' for item in self.added_domains)
        lines.append(f'Existing domains: {len(self.existing_domains)}')
        lines.extend(f'- {item}' for item in self.existing_domains)
        lines.append(f'Added IP/CIDR: {len(self.added_ips)}')
        lines.extend(f'- {item}' for item in self.added_ips)
        lines.append(f'Existing IP/CIDR: {len(self.existing_ips)}')
        lines.extend(f'- {item}' for item in self.existing_ips)
        return lines


@dataclass(frozen=True)
class RouteOverrides:
    node: str
    domains: tuple[str, ...]
    ips: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.domains and not self.ips

    def to_lines(self) -> list[str]:
        lines = [f'Route overrides for {self.node}: domains={len(self.domains)} ips={len(self.ips)}']
        lines.extend(f'- domain {domain}' for domain in self.domains)
        lines.extend(f'- ip {ip}' for ip in self.ips)
        return lines


class RouteOverrideStore:
    """Read/write local routing override YAML for generated Xray profiles."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_routes()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        except (OSError, yaml.YAMLError) as exc:
            raise RouteOverrideError(f'cannot read route overrides {self.path}: {exc}') from exc
        if not isinstance(raw, dict):
            raise RouteOverrideError(f'route overrides {self.path} must contain a YAML mapping')
        if raw.get('schema_version') != ROUTES_SCHEMA_VERSION:
            raise RouteOverrideError(f'unsupported routes schema_version: {raw.get("schema_version")!r}')
        raw.setdefault('nodes', {})
        if not isinstance(raw['nodes'], dict):
            raise RouteOverrideError('route overrides nodes must be a mapping')
        return raw

    def save(self, routes: dict[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        routes['updated_at'] = utc_now_iso()
        data = yaml.safe_dump(routes, sort_keys=False, allow_unicode=True)
        self.path.write_text(data, encoding='utf-8')
        return self.path

    def add(
        self,
        config: NodeConfig,
        *,
        domains: list[str],
        ips: list[str],
        comment: str | None = None,
    ) -> RouteAddResult:
        if config.role != NodeRole.RU_EDGE:
            raise RouteOverrideError('route overrides can only be attached to ru-edge cascade nodes')
        if not domains and not ips:
            raise RouteOverrideError('provide at least one --domain or --ip')

        normalized_domains = [_normalize_domain(item) for item in domains]
        normalized_ips = [_normalize_network(item) for item in ips]
        routes = self.load()
        node_routes = routes['nodes'].setdefault(
            config.display.internal_name,
            {'ru_direct_domains': [], 'ru_direct_ips': []},
        )
        if not isinstance(node_routes, dict):
            raise RouteOverrideError(f'route overrides for {config.display.internal_name} must be a mapping')
        domain_items = node_routes.setdefault('ru_direct_domains', [])
        ip_items = node_routes.setdefault('ru_direct_ips', [])
        if not isinstance(domain_items, list) or not isinstance(ip_items, list):
            raise RouteOverrideError(f'route overrides for {config.display.internal_name} must contain route lists')

        added_domains, existing_domains = _append_route_items(
            domain_items,
            'domain',
            normalized_domains,
            comment,
        )
        added_ips, existing_ips = _append_route_items(
            ip_items,
            'cidr',
            normalized_ips,
            comment,
        )
        path = self.save(routes)
        return RouteAddResult(
            path=path,
            node=config.display.internal_name,
            added_domains=tuple(added_domains),
            added_ips=tuple(added_ips),
            existing_domains=tuple(existing_domains),
            existing_ips=tuple(existing_ips),
        )

    def get_for_node(self, config: NodeConfig) -> RouteOverrides:
        if config.role != NodeRole.RU_EDGE:
            raise RouteOverrideError('route overrides can only be attached to ru-edge cascade nodes')
        routes = self.load()
        node_routes = routes.get('nodes', {}).get(config.display.internal_name, {})
        if not isinstance(node_routes, dict):
            raise RouteOverrideError(f'route overrides for {config.display.internal_name} must be a mapping')
        return RouteOverrides(
            node=config.display.internal_name,
            domains=tuple(_extract_route_values(node_routes.get('ru_direct_domains'), 'domain')),
            ips=tuple(_extract_route_values(node_routes.get('ru_direct_ips'), 'cidr')),
        )


def _append_route_items(
    items: list[dict[str, Any]],
    value_key: str,
    values: list[str],
    comment: str | None,
) -> tuple[list[str], list[str]]:
    existing_values = {str(item.get(value_key)) for item in items}
    added: list[str] = []
    existing: list[str] = []
    for value in values:
        if value in existing_values:
            existing.append(value)
            continue
        record: dict[str, Any] = {
            value_key: value,
            'added_at': utc_now_iso(),
        }
        if comment:
            record['comment'] = comment
        items.append(record)
        existing_values.add(value)
        added.append(value)
    return added, existing


def _extract_route_values(raw_items: Any, value_key: str) -> list[str]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise RouteOverrideError(f'route override {value_key} entries must be a list')
    values: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise RouteOverrideError(f'route override {value_key} entries must be mappings')
        value = item.get(value_key)
        if not isinstance(value, str) or not value:
            raise RouteOverrideError(f'route override {value_key} entry is missing a value')
        values.append(value)
    return values


def _normalize_domain(value: str) -> str:
    normalized = value.strip().lower().rstrip('.')
    if normalized.startswith('*.'):
        suffix = normalized[2:]
        if not DOMAIN_RE.fullmatch(suffix):
            raise RouteOverrideError(f'invalid wildcard domain: {value!r}')
        return f'*.{suffix}'
    if not DOMAIN_RE.fullmatch(normalized):
        raise RouteOverrideError(f'invalid domain: {value!r}')
    return normalized


def _normalize_network(value: str) -> str:
    try:
        return str(ip_network(value.strip(), strict=False))
    except ValueError as exc:
        raise RouteOverrideError(f'invalid IP/CIDR: {value!r}') from exc


def _empty_routes() -> dict[str, Any]:
    timestamp = utc_now_iso()
    return {
        'schema_version': ROUTES_SCHEMA_VERSION,
        'created_at': timestamp,
        'updated_at': timestamp,
        'nodes': {},
    }
