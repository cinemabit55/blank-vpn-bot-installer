"""Synthetic VPN client checks for generated/public node connectivity."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.templar_node.availability import AvailabilityCheck, AvailabilityResult, AvailabilityStatus
from app.templar_node.schemas import NodeConfig, WarpMode


DEFAULT_SYNTHETIC_PROBE_URL = 'https://www.cloudflare.com/cdn-cgi/trace'


class SyntheticVpnError(ValueError):
    """Raised when a synthetic VPN check cannot be assembled."""


@dataclass(frozen=True)
class SyntheticCommandResult:
    returncode: int
    stdout: str
    stderr: str


class SyntheticVpnRunner(Protocol):
    def fetch_via_xray(
        self,
        *,
        xray_bin: str,
        outbound: dict[str, Any],
        local_socks_port: int,
        url: str,
        timeout_seconds: int,
    ) -> SyntheticCommandResult:
        """Start a temporary Xray client and fetch URL through its SOCKS inbound."""


class SubprocessSyntheticVpnRunner:
    """Run the synthetic probe through local xray + curl binaries."""

    def fetch_via_xray(
        self,
        *,
        xray_bin: str,
        outbound: dict[str, Any],
        local_socks_port: int,
        url: str,
        timeout_seconds: int,
    ) -> SyntheticCommandResult:
        if shutil.which(xray_bin) is None and not Path(xray_bin).exists():
            return SyntheticCommandResult(127, '', f'xray binary not found: {xray_bin}')
        curl_bin = shutil.which('curl')
        if curl_bin is None:
            return SyntheticCommandResult(127, '', 'curl binary not found')
        port = local_socks_port or _free_local_port()
        client_config = _client_config(outbound, local_socks_port=port)
        with tempfile.TemporaryDirectory(prefix='templar-vpn-check-') as tmp_dir:
            config_path = Path(tmp_dir) / 'xray-client.json'
            config_path.write_text(json.dumps(client_config, ensure_ascii=False, indent=2), encoding='utf-8')
            process = subprocess.Popen(  # noqa: S603 - argv is explicit and contains no shell/user secrets.
                [xray_bin, 'run', '-config', str(config_path)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ready = _wait_for_port(port, timeout_seconds=min(timeout_seconds, 10))
                if not ready:
                    stdout, stderr = _terminate_process(process)
                    return SyntheticCommandResult(process.returncode or 1, stdout, stderr or 'xray SOCKS inbound did not become ready')
                completed = subprocess.run(  # noqa: S603 - argv is explicit and proxy URL is local.
                    [
                        curl_bin,
                        '-fsS',
                        '--max-time',
                        str(timeout_seconds),
                        '--socks5-hostname',
                        f'127.0.0.1:{port}',
                        url,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds + 5,
                    check=False,
                )
                return SyntheticCommandResult(completed.returncode, completed.stdout, completed.stderr)
            except (OSError, subprocess.SubprocessError) as exc:
                return SyntheticCommandResult(1, '', str(exc))
            finally:
                if process.poll() is None:
                    _terminate_process(process)


def run_synthetic_vpn_check(
    config: NodeConfig,
    *,
    client_config_path: str | Path,
    xray_bin: str = 'xray',
    local_socks_port: int = 18080,
    probe_url: str = DEFAULT_SYNTHETIC_PROBE_URL,
    expect_warp: bool | None = None,
    timeout_seconds: int = 20,
    runner: SyntheticVpnRunner | None = None,
) -> AvailabilityResult:
    """Run a real client-path smoke test: Xray client -> SOCKS -> HTTP probe."""

    checks: list[AvailabilityCheck] = []
    try:
        outbound = load_xray_outbound(client_config_path)
    except SyntheticVpnError as exc:
        checks.append(AvailabilityCheck('client_config', AvailabilityStatus.VPN_CLIENT_FAIL, str(exc)))
        return _result(config, checks)

    checks.append(AvailabilityCheck('client_config', AvailabilityStatus.OK, f'loaded outbound from {Path(client_config_path)}'))
    probe_runner = runner or SubprocessSyntheticVpnRunner()
    command = probe_runner.fetch_via_xray(
        xray_bin=xray_bin,
        outbound=outbound,
        local_socks_port=local_socks_port,
        url=probe_url,
        timeout_seconds=timeout_seconds,
    )
    if command.returncode != 0:
        checks.append(AvailabilityCheck('vpn_http', AvailabilityStatus.VPN_CLIENT_FAIL, _command_error(command)))
        return _result(config, checks)
    if not command.stdout.strip():
        checks.append(AvailabilityCheck('vpn_http', AvailabilityStatus.VPN_CLIENT_FAIL, 'probe response body is empty'))
        return _result(config, checks)

    checks.append(AvailabilityCheck('vpn_http', AvailabilityStatus.OK, f'probe fetched {len(command.stdout)} bytes via VPN client'))
    should_expect_warp = config.warp.mode == WarpMode.XRAY_NATIVE if expect_warp is None else expect_warp
    if should_expect_warp:
        checks.append(_warp_trace_check(command.stdout))
    return _result(config, checks)


def load_xray_outbound(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyntheticVpnError(f'cannot read Xray client config {path}: {exc}') from exc
    if not isinstance(raw, dict):
        raise SyntheticVpnError('Xray client config must be a JSON object')
    if isinstance(raw.get('outbound'), dict):
        return _validate_outbound(raw['outbound'])
    if isinstance(raw.get('outbounds'), list):
        for item in raw['outbounds']:
            if isinstance(item, dict) and item.get('protocol') not in {'freedom', 'blackhole'}:
                return _validate_outbound(item)
        raise SyntheticVpnError('Xray client config outbounds has no usable proxy outbound')
    if 'protocol' in raw and 'settings' in raw:
        return _validate_outbound(raw)
    raise SyntheticVpnError('Xray client config must contain an outbound object or outbounds list')


def _validate_outbound(outbound: dict[str, Any]) -> dict[str, Any]:
    protocol = outbound.get('protocol')
    if not isinstance(protocol, str) or not protocol:
        raise SyntheticVpnError('Xray outbound is missing protocol')
    if not isinstance(outbound.get('settings'), dict):
        raise SyntheticVpnError('Xray outbound is missing settings object')
    cloned = json.loads(json.dumps(outbound, ensure_ascii=False))
    cloned.setdefault('tag', 'synthetic_vpn_out')
    return cloned


def _client_config(outbound: dict[str, Any], *, local_socks_port: int) -> dict[str, Any]:
    outbound_tag = str(outbound.get('tag') or 'synthetic_vpn_out')
    outbound = dict(outbound)
    outbound['tag'] = outbound_tag
    return {
        'log': {'loglevel': 'warning'},
        'inbounds': [
            {
                'tag': 'synthetic_socks_in',
                'listen': '127.0.0.1',
                'port': local_socks_port,
                'protocol': 'socks',
                'settings': {'auth': 'noauth', 'udp': False},
                'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': False},
            },
        ],
        'outbounds': [outbound],
        'routing': {
            'domainStrategy': 'IPIfNonMatch',
            'rules': [
                {'type': 'field', 'inboundTag': ['synthetic_socks_in'], 'outboundTag': outbound_tag},
            ],
        },
    }


def _warp_trace_check(body: str) -> AvailabilityCheck:
    warp_value = _trace_value(body, 'warp')
    if warp_value in {'on', 'plus'}:
        return AvailabilityCheck('warp_egress', AvailabilityStatus.OK, f'cloudflare trace warp={warp_value}')
    if warp_value is None:
        return AvailabilityCheck('warp_egress', AvailabilityStatus.WARP_EGRESS_FAIL, 'cloudflare trace has no warp field')
    return AvailabilityCheck('warp_egress', AvailabilityStatus.WARP_EGRESS_FAIL, f'cloudflare trace warp={warp_value}')


def _trace_value(body: str, key: str) -> str | None:
    prefix = f'{key}='
    for line in body.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip().lower()
    return None


def _result(config: NodeConfig, checks: list[AvailabilityCheck]) -> AvailabilityResult:
    status = AvailabilityStatus.OK
    for check in checks:
        if not check.ok:
            status = check.status
            break
    return AvailabilityResult(
        internal_name=config.display.internal_name,
        vantage='synthetic-vpn-client',
        status=status,
        checks=tuple(checks),
    )


def _command_error(result: SyntheticCommandResult) -> str:
    return (result.stderr or result.stdout or f'exit code {result.returncode}').strip()


def _wait_for_port(port: int, *, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=3)
    return stdout or '', stderr or ''
