"""Read-only Cloudflare DNS checks for node-domain preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

from app.templar_node.http_json import JsonHttpClient, JsonHttpError


CLOUDFLARE_API_URL = 'https://api.cloudflare.com/client/v4'


class CloudflareCheckError(RuntimeError):
    """Raised when a Cloudflare read-only check cannot complete."""


class CloudflareUpsertError(RuntimeError):
    """Raised when Cloudflare DNS record upsert cannot complete safely."""


@dataclass(frozen=True)
class CloudflareZoneCheck:
    domain: str
    found: bool
    zone_id: str | None = None
    status: str | None = None
    paused: bool | None = None
    name_servers: tuple[str, ...] = ()
    dns_records_count: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.found and self.status == 'active'

    def to_dict(self) -> dict[str, Any]:
        return {
            'domain': self.domain,
            'found': self.found,
            'zone_id': self.zone_id,
            'status': self.status,
            'paused': self.paused,
            'name_servers': list(self.name_servers),
            'dns_records_count': self.dns_records_count,
            'warnings': list(self.warnings),
            'ok': self.ok,
        }


@dataclass(frozen=True)
class CloudflareCheckResult:
    zones: tuple[CloudflareZoneCheck, ...]

    @property
    def ok(self) -> bool:
        return all(zone.ok for zone in self.zones)

    def to_dict(self) -> dict[str, Any]:
        return {'ok': self.ok, 'zones': [zone.to_dict() for zone in self.zones]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_lines(self) -> list[str]:
        lines = [f'Cloudflare zones checked: {len(self.zones)}']
        for zone in self.zones:
            if not zone.found:
                lines.append(f'FAIL {zone.domain}: zone not found')
                continue
            status = 'OK' if zone.ok else 'WARN'
            lines.append(
                f'{status} {zone.domain}: status={zone.status} paused={zone.paused} records={zone.dns_records_count}',
            )
            if zone.name_servers:
                lines.append(f'  name_servers: {", ".join(zone.name_servers)}')
            lines.extend(f'  warning: {warning}' for warning in zone.warnings)
        return lines


@dataclass(frozen=True)
class CloudflareDnsRecord:
    record_id: str
    name: str
    record_type: str
    content: str
    ttl: int
    proxied: bool
    status: str
    zone_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            'record_id': self.record_id,
            'name': self.name,
            'record_type': self.record_type,
            'content': self.content,
            'ttl': self.ttl,
            'proxied': self.proxied,
            'status': self.status,
            'zone_name': self.zone_name,
        }


@dataclass(frozen=True)
class CloudflareUpsertResult:
    records: tuple[CloudflareDnsRecord, ...]

    @property
    def ok(self) -> bool:
        return all(record.status in {'created', 'updated', 'existing'} for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        return {'ok': self.ok, 'records': [record.to_dict() for record in self.records]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_lines(self) -> list[str]:
        lines = [f'Cloudflare DNS records touched: {len(self.records)}']
        for record in self.records:
            lines.append(
                f'{record.status.upper()} {record.record_type} {record.name} -> {record.content} '
                f'ttl={record.ttl} proxied={record.proxied} zone={record.zone_name}',
            )
        return lines


@dataclass(frozen=True)
class CloudflareDeleteResult:
    records: tuple[CloudflareDnsRecord, ...]

    @property
    def ok(self) -> bool:
        return all(record.status in {'deleted', 'missing'} for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        return {'ok': self.ok, 'records': [record.to_dict() for record in self.records]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_lines(self) -> list[str]:
        lines = [f'Cloudflare DNS records deleted: {sum(1 for record in self.records if record.status == "deleted")}']
        for record in self.records:
            target = f' -> {record.content}' if record.content else ''
            lines.append(f'{record.status.upper()} {record.record_type} {record.name}{target} zone={record.zone_name}')
        return lines


def run_cloudflare_check(
    *,
    domains: list[str],
    api_token: str,
    timeout_seconds: int = 20,
) -> CloudflareCheckResult:
    client = JsonHttpClient(
        base_url=CLOUDFLARE_API_URL,
        headers={'Accept': 'application/json', 'Authorization': f'Bearer {api_token}'},
        timeout_seconds=timeout_seconds,
    )
    checks = tuple(_check_zone(client, domain) for domain in domains)
    return CloudflareCheckResult(zones=checks)


def upsert_node_dns_records(
    *,
    fqdn: str,
    ipv4: str | None,
    ipv6: str | None,
    ttl: int,
    api_token: str,
    timeout_seconds: int = 20,
    proxied: bool = False,
) -> CloudflareUpsertResult:
    normalized_fqdn = _normalize_dns_name(fqdn)
    if ttl < 60 or ttl > 86400:
        raise CloudflareUpsertError('ttl must be between 60 and 86400 seconds')
    desired: list[tuple[str, str]] = []
    if ipv4:
        desired.append(('A', _normalize_ip(ipv4, version=4)))
    if ipv6:
        desired.append(('AAAA', _normalize_ip(ipv6, version=6)))
    if not desired:
        raise CloudflareUpsertError('at least one of ipv4 or ipv6 is required')

    client = JsonHttpClient(
        base_url=CLOUDFLARE_API_URL,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_token}',
        },
        timeout_seconds=timeout_seconds,
    )
    zone = _find_zone_for_fqdn(client, normalized_fqdn)
    records = tuple(
        _upsert_record(
            client,
            zone_id=zone['id'],
            zone_name=zone['name'],
            fqdn=normalized_fqdn,
            record_type=record_type,
            content=content,
            ttl=ttl,
            proxied=proxied,
        )
        for record_type, content in desired
    )
    return CloudflareUpsertResult(records=records)


def delete_node_dns_records(
    *,
    fqdn: str,
    ipv4: str | None,
    ipv6: str | None,
    api_token: str,
    timeout_seconds: int = 20,
) -> CloudflareDeleteResult:
    normalized_fqdn = _normalize_dns_name(fqdn)
    desired: list[tuple[str, str | None]] = []
    if ipv4:
        desired.append(('A', _normalize_ip(ipv4, version=4)))
    if ipv6:
        desired.append(('AAAA', _normalize_ip(ipv6, version=6)))
    if not desired:
        raise CloudflareUpsertError('at least one of ipv4 or ipv6 is required')

    client = JsonHttpClient(
        base_url=CLOUDFLARE_API_URL,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_token}',
        },
        timeout_seconds=timeout_seconds,
    )
    zone = _find_zone_for_fqdn(client, normalized_fqdn)
    records = tuple(
        _delete_record(
            client,
            zone_id=zone['id'],
            zone_name=zone['name'],
            fqdn=normalized_fqdn,
            record_type=record_type,
            expected_content=content,
        )
        for record_type, content in desired
    )
    return CloudflareDeleteResult(records=records)


def _check_zone(client: JsonHttpClient, domain: str) -> CloudflareZoneCheck:
    normalized = domain.strip().lower().rstrip('.')
    if not normalized:
        raise CloudflareCheckError('domain cannot be blank')
    try:
        zone_payload = client.get('/zones', params={'name': normalized, 'per_page': '1'})
    except JsonHttpError as exc:
        raise CloudflareCheckError(f'Cloudflare zone lookup failed for {normalized}: {exc}') from exc
    _ensure_cloudflare_success(zone_payload, f'zone lookup for {normalized}')
    result = zone_payload.get('result')
    if not isinstance(result, list) or not result:
        return CloudflareZoneCheck(domain=normalized, found=False)

    zone = result[0]
    if not isinstance(zone, dict):
        return CloudflareZoneCheck(domain=normalized, found=False)
    zone_id = str(zone.get('id') or '')
    records_count = _dns_records_count(client, zone_id) if zone_id else 0
    name_servers = tuple(str(item) for item in zone.get('name_servers') or [])
    warnings = _zone_warnings(zone, records_count)
    return CloudflareZoneCheck(
        domain=normalized,
        found=True,
        zone_id=zone_id or None,
        status=str(zone.get('status') or ''),
        paused=bool(zone.get('paused')),
        name_servers=name_servers,
        dns_records_count=records_count,
        warnings=warnings,
    )


def _find_zone_for_fqdn(client: JsonHttpClient, fqdn: str) -> dict[str, Any]:
    labels = fqdn.split('.')
    for index in range(max(0, len(labels) - 1)):
        candidate = '.'.join(labels[index:])
        try:
            payload = client.get('/zones', params={'name': candidate, 'per_page': '1'})
        except JsonHttpError as exc:
            raise CloudflareUpsertError(f'Cloudflare zone lookup failed for {candidate}: {exc}') from exc
        _ensure_cloudflare_upsert_success(payload, f'zone lookup for {candidate}')
        result = payload.get('result')
        if isinstance(result, list) and result and isinstance(result[0], dict):
            zone = result[0]
            zone_id = str(zone.get('id') or '')
            zone_name = str(zone.get('name') or candidate).lower()
            status = str(zone.get('status') or '')
            if not zone_id:
                raise CloudflareUpsertError(f'Cloudflare zone {candidate} has no id in API response')
            if status != 'active':
                raise CloudflareUpsertError(f'Cloudflare zone {candidate} status is {status or "<missing>"}, expected active')
            return {'id': zone_id, 'name': zone_name}
    raise CloudflareUpsertError(f'no active Cloudflare zone found for {fqdn}')


def _upsert_record(
    client: JsonHttpClient,
    *,
    zone_id: str,
    zone_name: str,
    fqdn: str,
    record_type: str,
    content: str,
    ttl: int,
    proxied: bool,
) -> CloudflareDnsRecord:
    existing = _find_dns_record(client, zone_id=zone_id, fqdn=fqdn, record_type=record_type)
    body = {
        'type': record_type,
        'name': fqdn,
        'content': content,
        'ttl': ttl,
        'proxied': proxied,
    }
    try:
        if existing is None:
            payload = client.post(f'/zones/{zone_id}/dns_records', json_body=body)
            status = 'created'
        else:
            same = (
                str(existing.get('content') or '') == content
                and int(existing.get('ttl') or ttl) == ttl
                and bool(existing.get('proxied')) == proxied
            )
            if same:
                return CloudflareDnsRecord(
                    record_id=str(existing.get('id') or ''),
                    name=fqdn,
                    record_type=record_type,
                    content=content,
                    ttl=ttl,
                    proxied=proxied,
                    status='existing',
                    zone_name=zone_name,
                )
            payload = client.put(f'/zones/{zone_id}/dns_records/{existing["id"]}', json_body=body)
            status = 'updated'
    except (KeyError, JsonHttpError) as exc:
        raise CloudflareUpsertError(f'Cloudflare DNS upsert failed for {record_type} {fqdn}: {exc}') from exc

    _ensure_cloudflare_upsert_success(payload, f'DNS upsert for {record_type} {fqdn}')
    result = payload.get('result')
    if not isinstance(result, dict) or not result.get('id'):
        raise CloudflareUpsertError(f'Cloudflare DNS upsert for {record_type} {fqdn} returned no record id')
    return CloudflareDnsRecord(
        record_id=str(result['id']),
        name=fqdn,
        record_type=record_type,
        content=str(result.get('content') or content),
        ttl=int(result.get('ttl') or ttl),
        proxied=bool(result.get('proxied')),
        status=status,
        zone_name=zone_name,
    )


def _delete_record(
    client: JsonHttpClient,
    *,
    zone_id: str,
    zone_name: str,
    fqdn: str,
    record_type: str,
    expected_content: str | None,
) -> CloudflareDnsRecord:
    existing = _find_dns_record(client, zone_id=zone_id, fqdn=fqdn, record_type=record_type)
    if existing is None:
        return CloudflareDnsRecord('', fqdn, record_type, expected_content or '', 1, False, 'missing', zone_name)
    content = str(existing.get('content') or '')
    if expected_content and content and content != expected_content:
        raise CloudflareUpsertError(
            f'Cloudflare DNS delete refused for {record_type} {fqdn}: content={content!r} expected={expected_content!r}',
        )
    record_id = str(existing.get('id') or '')
    if not record_id:
        raise CloudflareUpsertError(f'Cloudflare DNS delete failed for {record_type} {fqdn}: record has no id')
    try:
        payload = client.delete(f'/zones/{zone_id}/dns_records/{record_id}')
    except JsonHttpError as exc:
        if exc.status_code == 404:
            status = 'missing'
        else:
            raise CloudflareUpsertError(f'Cloudflare DNS delete failed for {record_type} {fqdn}: {exc}') from exc
    else:
        _ensure_cloudflare_upsert_success(payload, f'DNS delete for {record_type} {fqdn}')
        status = 'deleted'
    return CloudflareDnsRecord(
        record_id=record_id,
        name=fqdn,
        record_type=record_type,
        content=content,
        ttl=int(existing.get('ttl') or 1),
        proxied=bool(existing.get('proxied')),
        status=status,
        zone_name=zone_name,
    )


def _find_dns_record(
    client: JsonHttpClient,
    *,
    zone_id: str,
    fqdn: str,
    record_type: str,
) -> dict[str, Any] | None:
    try:
        payload = client.get(
            f'/zones/{zone_id}/dns_records',
            params={'type': record_type, 'name': fqdn, 'per_page': '2'},
        )
    except JsonHttpError as exc:
        raise CloudflareUpsertError(f'Cloudflare DNS lookup failed for {record_type} {fqdn}: {exc}') from exc
    _ensure_cloudflare_upsert_success(payload, f'DNS record lookup for {record_type} {fqdn}')
    result = payload.get('result')
    if not isinstance(result, list) or not result:
        return None
    if len(result) > 1:
        raise CloudflareUpsertError(f'multiple {record_type} records already exist for {fqdn}; clean them manually first')
    record = result[0]
    return record if isinstance(record, dict) else None


def _dns_records_count(client: JsonHttpClient, zone_id: str) -> int:
    try:
        payload = client.get(f'/zones/{zone_id}/dns_records', params={'per_page': '1'})
    except JsonHttpError:
        return 0
    _ensure_cloudflare_success(payload, f'DNS records lookup for zone {zone_id}')
    info = payload.get('result_info')
    if isinstance(info, dict):
        total = info.get('total_count')
        if isinstance(total, int):
            return total
    result = payload.get('result')
    return len(result) if isinstance(result, list) else 0


def _normalize_dns_name(value: str) -> str:
    normalized = value.strip().lower().rstrip('.')
    if not normalized or '.' not in normalized:
        raise CloudflareUpsertError(f'invalid DNS name: {value!r}')
    return normalized


def _normalize_ip(value: str, *, version: int) -> str:
    parsed = ip_address(value.strip())
    if parsed.version != version:
        raise CloudflareUpsertError(f'expected IPv{version} address for {value!r}')
    return str(parsed)


def _zone_warnings(zone: dict[str, Any], records_count: int) -> tuple[str, ...]:
    warnings: list[str] = []
    status = str(zone.get('status') or '')
    if status != 'active':
        warnings.append(f'zone status is {status or "<missing>"}, expected active')
    if bool(zone.get('paused')):
        warnings.append('zone is paused')
    if not zone.get('name_servers'):
        warnings.append('zone has no Cloudflare name servers in API response')
    if records_count == 0:
        warnings.append('zone has no DNS records yet')
    return tuple(warnings)


def _ensure_cloudflare_upsert_success(payload: dict[str, Any], operation: str) -> None:
    try:
        _ensure_cloudflare_success(payload, operation)
    except CloudflareCheckError as exc:
        raise CloudflareUpsertError(str(exc)) from exc


def _ensure_cloudflare_success(payload: dict[str, Any], operation: str) -> None:
    if payload.get('success', True):
        return
    errors = payload.get('errors')
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get('message'):
            raise CloudflareCheckError(f'Cloudflare {operation} failed: {first["message"]}')
        raise CloudflareCheckError(f'Cloudflare {operation} failed: {first}')
    raise CloudflareCheckError(f'Cloudflare {operation} failed')
