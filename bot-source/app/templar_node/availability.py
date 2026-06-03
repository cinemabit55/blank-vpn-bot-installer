"""Alert-only availability checks for node public endpoints."""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.templar_node.schemas import NodeConfig, NodeRole, RealityStrategy


class AvailabilityStatus(StrEnum):
    OK = 'OK'
    DNS_FAIL = 'DNS_FAIL'
    DNS_WRONG_IP = 'DNS_WRONG_IP'
    TCP_443_FAIL = 'TCP_443_FAIL'
    TLS_FAIL = 'TLS_FAIL'
    DECOY_HTTP_FAIL = 'DECOY_HTTP_FAIL'
    CERT_EXPIRING = 'CERT_EXPIRING'
    REMNA_NODE_OFFLINE = 'REMNA_NODE_OFFLINE'
    VPN_CLIENT_FAIL = 'VPN_CLIENT_FAIL'
    WARP_EGRESS_FAIL = 'WARP_EGRESS_FAIL'
    TRANSIT_10443_FAIL = 'TRANSIT_10443_FAIL'
    RU_PROBE_OFFLINE = 'RU_PROBE_OFFLINE'
    POSSIBLE_RKN_DOMAIN_BLOCK = 'POSSIBLE_RKN_DOMAIN_BLOCK'
    POSSIBLE_RKN_IP_BLOCK = 'POSSIBLE_RKN_IP_BLOCK'
    SERVER_DOWN = 'SERVER_DOWN'
    UNKNOWN_DEGRADED = 'UNKNOWN_DEGRADED'
    SKIPPED = 'SKIPPED'


@dataclass(frozen=True)
class AvailabilityCheck:
    name: str
    status: AvailabilityStatus
    message: str

    @property
    def ok(self) -> bool:
        return self.status in {AvailabilityStatus.OK, AvailabilityStatus.SKIPPED}

    def to_dict(self) -> dict[str, Any]:
        return {'name': self.name, 'status': self.status.value, 'message': self.message, 'ok': self.ok}


@dataclass(frozen=True)
class AvailabilityResult:
    internal_name: str
    vantage: str
    status: AvailabilityStatus
    checks: tuple[AvailabilityCheck, ...]

    @property
    def ok(self) -> bool:
        return self.status == AvailabilityStatus.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            'internal_name': self.internal_name,
            'vantage': self.vantage,
            'status': self.status.value,
            'ok': self.ok,
            'checks': [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_lines(self) -> list[str]:
        lines = [
            f'Availability check: {self.internal_name}',
            f'Vantage: {self.vantage}',
            f'Status: {self.status.value}',
        ]
        for check in self.checks:
            marker = 'OK' if check.ok else 'FAIL'
            lines.append(f'{marker} {check.name}: {check.status.value} - {check.message}')
        return lines


Probe = Callable[[NodeConfig, int], AvailabilityCheck]
RemoteProbe = Callable[[NodeConfig, int, 'RemoteCommandRunner'], AvailabilityCheck]


@dataclass(frozen=True)
class RemoteCommandResult:
    returncode: int
    stdout: str
    stderr: str


class RemoteCommandRunner(Protocol):
    def run(self, script: str, *, timeout_seconds: int) -> RemoteCommandResult:
        """Run a shell probe on a remote vantage point."""


class SshRemoteCommandRunner:
    """Run alert-only probes through an already-provisioned SSH admin key."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        private_key: str,
        port: int = 22,
    ):
        self.host = host
        self.user = user
        self.private_key = private_key
        self.port = port

    def run(self, script: str, *, timeout_seconds: int) -> RemoteCommandResult:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as key_file:
            key_file.write(self.private_key.rstrip() + '\n')
            key_path = Path(key_file.name)
        try:
            key_path.chmod(0o600)
            completed = subprocess.run(  # noqa: S603 - argv is fixed and script is passed on stdin.
                [
                    '/usr/bin/ssh',
                    '-i',
                    str(key_path),
                    '-p',
                    str(self.port),
                    '-o',
                    'BatchMode=yes',
                    '-o',
                    'IdentitiesOnly=yes',
                    '-o',
                    'StrictHostKeyChecking=accept-new',
                    f'{self.user}@{self.host}',
                    'bash -s',
                ],
                input=script,
                text=True,
                capture_output=True,
                timeout=timeout_seconds + 10,
                check=False,
            )
            return RemoteCommandResult(completed.returncode, completed.stdout, completed.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            return RemoteCommandResult(255, '', str(exc))
        finally:
            key_path.unlink(missing_ok=True)


def run_main_availability_check(
    config: NodeConfig,
    *,
    timeout_seconds: int = 5,
    probes: Mapping[str, Probe] | None = None,
) -> AvailabilityResult:
    """Run main/control-plane endpoint checks from the current foreign vantage point."""

    probe_map = dict(probes or _default_probe_map(config))
    checks: list[AvailabilityCheck] = []
    for name in _probe_order(config):
        probe = probe_map.get(name)
        if probe is None:
            continue
        check = probe(config, timeout_seconds)
        checks.append(check)
        if not check.ok and check.status in {
            AvailabilityStatus.DNS_FAIL,
            AvailabilityStatus.DNS_WRONG_IP,
            AvailabilityStatus.TCP_443_FAIL,
            AvailabilityStatus.TLS_FAIL,
        }:
            break
    return AvailabilityResult(
        internal_name=config.display.internal_name,
        vantage='main-foreign',
        status=_overall_status(checks),
        checks=tuple(checks),
    )


def run_ru_edge_foreign_exit_check(
    config: NodeConfig,
    *,
    runner: RemoteCommandRunner,
    timeout_seconds: int = 5,
    probes: Mapping[str, RemoteProbe] | None = None,
) -> AvailabilityResult:
    """Run alert-only foreign-exit checks from an already bootstrapped RU-edge node."""

    if config.role != NodeRole.FOREIGN_EXIT:
        return AvailabilityResult(
            internal_name=config.display.internal_name,
            vantage='ru-edge',
            status=AvailabilityStatus.SKIPPED,
            checks=(
                AvailabilityCheck(
                    'role',
                    AvailabilityStatus.SKIPPED,
                    f'RU-edge foreign-exit checks require role=foreign-exit, got {config.role.value}',
                ),
            ),
        )
    probe_map = dict(probes or _default_remote_probe_map(config))
    checks: list[AvailabilityCheck] = []
    for name in _remote_probe_order(config):
        probe = probe_map.get(name)
        if probe is None:
            continue
        check = probe(config, timeout_seconds, runner)
        checks.append(check)
        if not check.ok and check.status in {
            AvailabilityStatus.RU_PROBE_OFFLINE,
            AvailabilityStatus.POSSIBLE_RKN_DOMAIN_BLOCK,
            AvailabilityStatus.POSSIBLE_RKN_IP_BLOCK,
            AvailabilityStatus.TLS_FAIL,
        }:
            break
    return AvailabilityResult(
        internal_name=config.display.internal_name,
        vantage='ru-edge',
        status=_overall_status(checks),
        checks=tuple(checks),
    )


def _probe_order(config: NodeConfig) -> tuple[str, ...]:
    transit_probe = ('transit_10443',) if config.host.inbound_ref == 'transit' and config.transit.listen_port else ()
    if config.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE:
        return ('dns', 'tcp_443', 'tls', 'decoy_http', *transit_probe)
    return ('tcp_443', *transit_probe)


def _default_probe_map(config: NodeConfig) -> dict[str, Probe]:
    return {
        'dns': _dns_probe,
        'tcp_443': _tcp_probe,
        'tls': _tls_probe,
        'decoy_http': _decoy_http_probe,
        'transit_10443': _transit_probe,
    }


def _remote_probe_order(config: NodeConfig) -> tuple[str, ...]:
    names = ['dns', 'tcp_443', 'tls']
    if config.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE:
        names.append('decoy_http')
    if config.transit.listen_port:
        names.append('transit_10443')
    return tuple(names)


def _default_remote_probe_map(config: NodeConfig) -> dict[str, RemoteProbe]:
    return {
        'dns': _remote_dns_probe,
        'tcp_443': _remote_tcp_probe,
        'tls': _remote_tls_probe,
        'decoy_http': _remote_decoy_http_probe,
        'transit_10443': _remote_transit_probe,
    }


def _overall_status(checks: list[AvailabilityCheck]) -> AvailabilityStatus:
    for check in checks:
        if not check.ok:
            return check.status
    return AvailabilityStatus.OK


def _dns_probe(config: NodeConfig, timeout_seconds: int) -> AvailabilityCheck:
    del timeout_seconds
    host = config.effective_host_address()
    expected = _expected_probe_ips(config)
    try:
        resolved = _resolve_ips(host, _public_endpoint_port(config))
    except OSError as exc:
        return AvailabilityCheck('dns', AvailabilityStatus.DNS_FAIL, str(exc))
    if expected:
        address_is_override = config.host.address is not None and config.host.address != config.domain
        if address_is_override:
            wrong_ip = not expected.intersection(resolved)
        else:
            wrong_ip = not expected.issubset(resolved)
        if wrong_ip:
            return AvailabilityCheck(
                'dns',
                AvailabilityStatus.DNS_WRONG_IP,
                f'resolved={sorted(resolved)} expected={sorted(expected)}',
            )
    return AvailabilityCheck('dns', AvailabilityStatus.OK, f'resolved={sorted(resolved)}')


def _tcp_probe(config: NodeConfig, timeout_seconds: int) -> AvailabilityCheck:
    errors: list[str] = []
    attempt_timeout = min(timeout_seconds, 5)
    port = _public_endpoint_port(config)
    for label, host in _main_connect_targets(config):
        try:
            with socket.create_connection((host, port), timeout=attempt_timeout):
                return AvailabilityCheck('tcp_443', AvailabilityStatus.OK, f'{host}:{port} reachable via {label}')
        except OSError as exc:
            errors.append(f'{label}:{exc}')
    return AvailabilityCheck('tcp_443', AvailabilityStatus.TCP_443_FAIL, '; '.join(errors) or 'no connect targets')


def _tls_probe(config: NodeConfig, timeout_seconds: int) -> AvailabilityCheck:
    sni_host = _tls_hostname(config)
    errors: list[str] = []
    attempt_timeout = min(timeout_seconds, 5)
    port = _public_endpoint_port(config)
    for label, host in _main_connect_targets(config):
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=attempt_timeout) as raw_socket:
                with context.wrap_socket(raw_socket, server_hostname=sni_host) as tls_socket:
                    cert = tls_socket.getpeercert()
            break
        except (OSError, ssl.SSLError) as exc:
            errors.append(f'{label}:{exc}')
    else:
        return AvailabilityCheck('tls', AvailabilityStatus.TLS_FAIL, '; '.join(errors) or 'no connect targets')
    not_after = str(cert.get('notAfter') or '')
    if not_after:
        try:
            expires_at = datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=UTC)
        except ValueError:
            return AvailabilityCheck('tls', AvailabilityStatus.TLS_FAIL, f'cannot parse certificate notAfter={not_after!r}')
        if expires_at <= datetime.now(UTC) + timedelta(days=14):
            return AvailabilityCheck('tls', AvailabilityStatus.CERT_EXPIRING, f'certificate expires at {expires_at.isoformat()}')
    return AvailabilityCheck('tls', AvailabilityStatus.OK, f'certificate notAfter={not_after or "unknown"}')


def _decoy_http_probe(config: NodeConfig, timeout_seconds: int) -> AvailabilityCheck:
    sni_host = _tls_hostname(config)
    errors: list[str] = []
    port = _public_endpoint_port(config)
    for label, host in _main_connect_targets(config):
        try:
            status, body = _https_get(
                connect_host=host,
                sni_host=sni_host,
                port=port,
                timeout_seconds=min(timeout_seconds, 5),
            )
            break
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            errors.append(f'{label}:{exc}')
    else:
        return AvailabilityCheck('decoy_http', AvailabilityStatus.DECOY_HTTP_FAIL, '; '.join(errors) or 'no connect targets')
    if status >= 500:
        return AvailabilityCheck('decoy_http', AvailabilityStatus.DECOY_HTTP_FAIL, f'HTTP {status}')
    title = config.site.title
    if title and title not in body:
        return AvailabilityCheck('decoy_http', AvailabilityStatus.UNKNOWN_DEGRADED, 'decoy title not found in response body')
    return AvailabilityCheck('decoy_http', AvailabilityStatus.OK, f'HTTP {status}')


def _transit_probe(config: NodeConfig, timeout_seconds: int) -> AvailabilityCheck:
    if not config.transit.listen_port:
        return AvailabilityCheck('transit_10443', AvailabilityStatus.SKIPPED, 'no transit listen_port configured')
    errors: list[str] = []
    attempt_timeout = min(timeout_seconds, 5)
    for label, host in _main_connect_targets(config):
        try:
            with socket.create_connection((host, config.transit.listen_port), timeout=attempt_timeout):
                return AvailabilityCheck(
                    'transit_10443',
                    AvailabilityStatus.OK,
                    f'{host}:{config.transit.listen_port} reachable via {label}',
                )
        except OSError as exc:
            errors.append(f'{label}:{exc}')
    return AvailabilityCheck('transit_10443', AvailabilityStatus.TRANSIT_10443_FAIL, '; '.join(errors) or 'no connect targets')


def _public_dns_expected_ips(config: NodeConfig) -> set[str]:
    if config.public_ipv4:
        return {config.public_ipv4}
    return {config.public_ipv6} if config.public_ipv6 else set()


def _expected_probe_ips(config: NodeConfig) -> set[str]:
    known_ips = {ip for ip in (config.public_ipv4, config.public_ipv6) if ip}
    if config.host.address is not None and config.host.address != config.domain:
        if config.host.address in known_ips:
            return {config.host.address}
        return known_ips
    return _public_dns_expected_ips(config)


def _main_connect_targets(config: NodeConfig) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, host in (
        ('ipv4', config.public_ipv4),
        ('ipv6', config.public_ipv6),
        ('host', config.effective_host_address()),
    ):
        if host and host not in seen:
            targets.append((label, host))
            seen.add(host)
    return targets


def _https_get(*, connect_host: str, sni_host: str, port: int, timeout_seconds: int) -> tuple[int, str]:
    last_error: Exception | None = None
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(connect_host, port, type=socket.SOCK_STREAM):
        try:
            raw_socket = socket.socket(family, socktype, proto)
            raw_socket.settimeout(timeout_seconds)
            raw_socket.connect(sockaddr)
            context = ssl.create_default_context()
            with context.wrap_socket(raw_socket, server_hostname=sni_host) as tls_socket:
                tls_socket.settimeout(timeout_seconds)
                request = (
                    f'GET / HTTP/1.1\r\n'
                    f'Host: {sni_host}\r\n'
                    'User-Agent: templar-node-monitor/0.1\r\n'
                    'Accept: */*\r\n'
                    'Connection: close\r\n\r\n'
                )
                tls_socket.sendall(request.encode('ascii'))
                chunks: list[bytes] = []
                total = 0
                while total < 16384:
                    chunk = tls_socket.recv(min(4096, 16384 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            response = b''.join(chunks)
            headers, _, body = response.partition(b'\r\n\r\n')
            status_line = headers.splitlines()[0].decode('iso-8859-1', errors='replace')
            status = int(status_line.split()[1])
            return status, body.decode('utf-8', errors='replace')
        except (OSError, ssl.SSLError, IndexError, ValueError) as exc:
            last_error = exc
    if last_error is not None:
        raise OSError(str(last_error))
    raise OSError(f'no A/AAAA records resolved for {connect_host}')


def _tls_hostname(config: NodeConfig) -> str:
    return config.effective_reality_server_names()[0]


def _resolve_ips(host: str, port: int) -> set[str]:
    resolved: set[str] = set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if family in {socket.AF_INET, socket.AF_INET6}:
            resolved.add(str(sockaddr[0]))
    if not resolved:
        raise OSError('no A/AAAA records resolved')
    return resolved


def _remote_dns_probe(config: NodeConfig, timeout_seconds: int, runner: RemoteCommandRunner) -> AvailabilityCheck:
    host = config.effective_host_address()
    result = runner.run(
        f"""set -euo pipefail
timeout {timeout_seconds} bash -lc {sh_quote(f'(getent ahosts {sh_quote(host)} || getent hosts {sh_quote(host)})')} | awk '{{print $1}}' | sort -u
""",
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        return AvailabilityCheck('ru_dns', AvailabilityStatus.POSSIBLE_RKN_DOMAIN_BLOCK, _remote_error(result))
    resolved = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not resolved:
        return AvailabilityCheck('ru_dns', AvailabilityStatus.POSSIBLE_RKN_DOMAIN_BLOCK, 'no A/AAAA records resolved from RU edge')
    expected = _expected_probe_ips(config)
    if expected:
        address_is_override = config.host.address is not None and config.host.address != config.domain
        wrong_ip = not expected.intersection(resolved) if address_is_override else not expected.issubset(resolved)
        if wrong_ip:
            return AvailabilityCheck(
                'ru_dns',
                AvailabilityStatus.POSSIBLE_RKN_DOMAIN_BLOCK,
                f'ru_resolved={sorted(resolved)} expected={sorted(expected)}',
            )
    return AvailabilityCheck('ru_dns', AvailabilityStatus.OK, f'ru_resolved={sorted(resolved)}')


def _remote_tcp_probe(config: NodeConfig, timeout_seconds: int, runner: RemoteCommandRunner) -> AvailabilityCheck:
    return _remote_tcp_port_probe(
        runner,
        name='ru_tcp_443',
        host=config.effective_host_address(),
        port=_public_endpoint_port(config),
        timeout_seconds=timeout_seconds,
        failure_status=AvailabilityStatus.POSSIBLE_RKN_IP_BLOCK,
    )


def _remote_tls_probe(config: NodeConfig, timeout_seconds: int, runner: RemoteCommandRunner) -> AvailabilityCheck:
    host = config.effective_host_address()
    sni_host = _tls_hostname(config)
    attempts = _remote_tls_attempts(config)
    result = runner.run(
        f"""set -euo pipefail
for attempt in {attempts}; do
  label=${{attempt%%|*}}
  connect=${{attempt#*|}}
  if timeout {timeout_seconds} openssl s_client -connect "$connect" -servername {sh_quote(sni_host)} </dev/null >/dev/null 2>/tmp/templar-ru-tls.err; then
    echo "$label"
    exit 0
  fi
done
cat /tmp/templar-ru-tls.err >&2 || true
exit 1
""",
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        return AvailabilityCheck('ru_tls', AvailabilityStatus.TLS_FAIL, _remote_error(result))
    return AvailabilityCheck(
        'ru_tls',
        AvailabilityStatus.OK,
        f'{host}:{_public_endpoint_port(config)} TLS reachable from RU edge via {result.stdout.strip() or "domain"} sni={sni_host}',
    )


def _remote_decoy_http_probe(config: NodeConfig, timeout_seconds: int, runner: RemoteCommandRunner) -> AvailabilityCheck:
    host = _tls_hostname(config)
    attempts = _remote_curl_resolve_attempts(config)
    result = runner.run(
        f"""set -euo pipefail
for attempt in {attempts}; do
  label=${{attempt%%|*}}
  resolve=${{attempt#*|}}
  if [ "$resolve" = "-" ]; then
    resolve_arg=""
  else
    resolve_arg="--resolve $resolve"
  fi
  if timeout {timeout_seconds} curl -fsS --max-time {timeout_seconds} -A templar-node-monitor/0.1 $resolve_arg {sh_quote(f'https://{host}/')} > /tmp/templar-ru-decoy.body 2>/tmp/templar-ru-decoy.err; then
    echo "__templar_route=$label"
    head -c 4096 /tmp/templar-ru-decoy.body
    exit 0
  fi
done
cat /tmp/templar-ru-decoy.err >&2 || true
exit 1
""",
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        return AvailabilityCheck('ru_decoy_http', AvailabilityStatus.DECOY_HTTP_FAIL, _remote_error(result))
    if config.site.title and config.site.title not in result.stdout:
        return AvailabilityCheck('ru_decoy_http', AvailabilityStatus.UNKNOWN_DEGRADED, 'decoy title not found from RU edge')
    route = _extract_route_label(result.stdout)
    return AvailabilityCheck('ru_decoy_http', AvailabilityStatus.OK, f'decoy HTTP reachable from RU edge via {route}')


def _remote_transit_probe(config: NodeConfig, timeout_seconds: int, runner: RemoteCommandRunner) -> AvailabilityCheck:
    if not config.transit.listen_port:
        return AvailabilityCheck('ru_transit_10443', AvailabilityStatus.SKIPPED, 'no transit listen_port configured')
    return _remote_tcp_port_probe(
        runner,
        name='ru_transit_10443',
        host=config.public_ipv6 or config.public_ipv4,
        port=config.transit.listen_port,
        timeout_seconds=timeout_seconds,
        failure_status=AvailabilityStatus.TRANSIT_10443_FAIL,
    )


def _remote_tls_attempts(config: NodeConfig) -> str:
    host = config.effective_host_address()
    port = _public_endpoint_port(config)
    attempts = []
    if config.public_ipv6:
        attempts.append(('ipv6', _format_connect_host(config.public_ipv6, port)))
    if config.public_ipv4:
        attempts.append(('ipv4', _format_connect_host(config.public_ipv4, port)))
    attempts.append(('domain', _format_connect_host(host, port)))
    return ' '.join(sh_quote(f'{label}|{connect}') for label, connect in attempts)


def _remote_curl_resolve_attempts(config: NodeConfig) -> str:
    host = _tls_hostname(config)
    port = _public_endpoint_port(config)
    attempts = []
    if config.public_ipv6:
        attempts.append(('ipv6', f'{host}:{port}:[{config.public_ipv6}]'))
    if config.public_ipv4:
        attempts.append(('ipv4', f'{host}:{port}:{config.public_ipv4}'))
    attempts.append(('domain', '-'))
    return ' '.join(sh_quote(f'{label}|{resolve}') for label, resolve in attempts)


def _format_connect_host(host: str, port: int) -> str:
    if ':' in host and not host.startswith('['):
        return f'[{host}]:{port}'
    return f'{host}:{port}'


def _public_endpoint_port(config: NodeConfig) -> int:
    return config.reality.public_port


def _extract_route_label(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith('__templar_route='):
            return line.partition('=')[2] or 'domain'
    return 'domain'


def _remote_tcp_port_probe(
    runner: RemoteCommandRunner,
    *,
    name: str,
    host: str,
    port: int,
    timeout_seconds: int,
    failure_status: AvailabilityStatus,
) -> AvailabilityCheck:
    result = runner.run(
        f"""set -euo pipefail
HOST={sh_quote(host)}
PORT={int(port)}
export HOST PORT
timeout {timeout_seconds} bash -lc 'cat </dev/null >/dev/tcp/"$HOST"/"$PORT"'
""",
        timeout_seconds=timeout_seconds,
    )
    if result.returncode != 0:
        return AvailabilityCheck(name, failure_status, _remote_error(result))
    return AvailabilityCheck(name, AvailabilityStatus.OK, f'{host}:{port} reachable from RU edge')


def _remote_error(result: RemoteCommandResult) -> str:
    return (result.stderr or result.stdout or f'exit code {result.returncode}').strip()


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
