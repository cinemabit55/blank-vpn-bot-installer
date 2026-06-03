#!/usr/bin/env python3
"""Run Templar node availability checks and notify the admin Telegram chat.

The monitor is intentionally host-friendly: it reuses scripts/templar_node.py
checks, stores a small JSON state file, and sends alerts through the same bot
chat/topic configured by ADMIN_NOTIFICATIONS_* environment values.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - JSON config remains supported without PyYAML.
    yaml = None


DEFAULT_NOTIFY_COOLDOWN_SECONDS = 30 * 60
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_SYNTHETIC_INTERVAL_SECONDS = 15 * 60
TELEGRAM_API_BASE = 'https://api.telegram.org'


@dataclass(frozen=True)
class MonitorSettings:
    config_path: Path
    repo_dir: Path
    node_cli: Path
    python: str
    state_file: Path
    env_file: Path | None
    notify: bool
    notify_cooldown_seconds: int
    timeout_seconds: int
    synthetic_interval_seconds: int
    chat_id: str | None
    topic_id: int | None
    bot_token: str | None
    checks: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CheckRun:
    check_id: str
    check_type: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    result: dict[str, Any]
    checked_at: str

    @property
    def ok(self) -> bool:
        return bool(self.result.get('ok'))

    @property
    def status(self) -> str:
        return str(self.result.get('status') or ('OK' if self.ok else 'UNKNOWN_DEGRADED'))

    @property
    def title(self) -> str:
        return str(self.result.get('internal_name') or self.check_id)

    @property
    def vantage(self) -> str:
        return str(self.result.get('vantage') or self.check_type)


class MonitorError(RuntimeError):
    """Raised when monitor configuration or execution is invalid."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run Templar node checks and send Telegram admin alerts.')
    parser.add_argument('--config', type=Path, required=True, help='YAML/JSON monitor config file.')
    parser.add_argument('--no-notify', action='store_true', help='Run checks without sending Telegram alerts.')
    parser.add_argument('--notify-first-ok', action='store_true', help='Send OK notification for checks first seen as healthy.')
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, notify=not args.no_notify)
        state = load_state(settings.state_file)
        now = utc_now_iso()
        changed = False

        for check in settings.checks:
            check_id = require_str(check, 'id')
            if not is_due(check, state, now, settings):
                print(f'SKIP {check_id}: interval not elapsed')
                continue

            run = run_check(settings, check)
            print(f'{"OK" if run.ok else "FAIL"} {run.check_id}: {run.status} ({run.vantage})')
            notification = build_notification(run, state, settings, notify_first_ok=args.notify_first_ok)
            notification_sent = False
            if notification and settings.notify:
                notification_sent = send_telegram_message(settings, notification)
                print(f'NOTIFY {run.check_id}: {"sent" if notification_sent else "failed"}')
            elif notification:
                print(f'NOTIFY {run.check_id}: suppressed by --no-notify')

            update_state(state, run, notification_sent)
            changed = True

        if changed:
            save_state(settings.state_file, state)
        return 0
    except MonitorError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


def load_settings(config_path: Path, *, notify: bool) -> MonitorSettings:
    config_path = config_path.expanduser().resolve()
    raw = load_config(config_path)
    repo_dir = Path(raw.get('repo_dir') or Path(__file__).resolve().parents[1]).expanduser().resolve()
    node_cli = Path(raw.get('node_cli') or repo_dir / 'scripts' / 'templar_node.py').expanduser().resolve()
    python = str(raw.get('python') or sys.executable)

    monitor = raw.get('monitor') or {}
    if not isinstance(monitor, dict):
        raise MonitorError('monitor section must be a mapping')
    state_file = Path(monitor.get('state_file') or '/var/lib/templar-node-test/monitor/state.json').expanduser()
    timeout_seconds = int(monitor.get('timeout_seconds') or DEFAULT_TIMEOUT_SECONDS)
    notify_cooldown_seconds = int(monitor.get('notify_cooldown_seconds') or DEFAULT_NOTIFY_COOLDOWN_SECONDS)
    synthetic_interval_seconds = int(
        monitor.get('synthetic_interval_seconds') or DEFAULT_SYNTHETIC_INTERVAL_SECONDS
    )

    telegram = raw.get('telegram') or {}
    if not isinstance(telegram, dict):
        raise MonitorError('telegram section must be a mapping')
    env_file_value = telegram.get('env_file') or raw.get('env_file')
    env_file = Path(env_file_value).expanduser() if env_file_value else None
    env = dict(os.environ)
    if env_file:
        env.update(load_env_file(env_file))

    bot_token = str(telegram.get('bot_token') or env.get(str(telegram.get('bot_token_env') or 'BOT_TOKEN')) or '').strip()
    chat_id = str(telegram.get('chat_id') or env.get(str(telegram.get('chat_id_env') or 'ADMIN_NOTIFICATIONS_CHAT_ID')) or '').strip()
    topic_id = resolve_topic_id(telegram, env)

    raw_checks = raw.get('checks') or []
    if not isinstance(raw_checks, list) or not raw_checks:
        raise MonitorError('checks section must contain at least one check')
    checks: list[dict[str, Any]] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            raise MonitorError('each check must be a mapping')
        if item.get('enabled', True):
            checks.append(item)
    if not checks:
        raise MonitorError('all checks are disabled')

    if notify and (not bot_token or not chat_id):
        raise MonitorError('Telegram notify is enabled, but BOT_TOKEN or ADMIN_NOTIFICATIONS_CHAT_ID is missing')

    return MonitorSettings(
        config_path=config_path,
        repo_dir=repo_dir,
        node_cli=node_cli,
        python=python,
        state_file=state_file,
        env_file=env_file,
        notify=notify,
        notify_cooldown_seconds=notify_cooldown_seconds,
        timeout_seconds=timeout_seconds,
        synthetic_interval_seconds=synthetic_interval_seconds,
        chat_id=chat_id or None,
        topic_id=topic_id,
        bot_token=bot_token or None,
        checks=tuple(checks),
    )


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise MonitorError(f'config file does not exist: {path}')
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise MonitorError(f'cannot read config {path}: {exc}') from exc
    try:
        if path.suffix.lower() == '.json':
            raw = json.loads(text)
        else:
            if yaml is None:
                raise MonitorError('PyYAML is required for YAML monitor configs; use JSON or install PyYAML')
            raw = yaml.safe_load(text)
    except MonitorError:
        raise
    except Exception as exc:
        raise MonitorError(f'cannot parse config {path}: {exc}') from exc
    if not isinstance(raw, dict):
        raise MonitorError('monitor config must contain a mapping')
    return raw


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        raise MonitorError(f'env file does not exist: {path}')
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        raise MonitorError(f'cannot read env file {path}: {exc}') from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            parts = [line]
        if parts and '=' in parts[0]:
            key, value = parts[0].split('=', 1)
        else:
            key, value = line.split('=', 1)
            value = value.strip().strip('"').strip("'")
        key = key.strip()
        if key:
            values[key] = value
    return values


def resolve_topic_id(telegram: dict[str, Any], env: dict[str, str]) -> int | None:
    direct = telegram.get('topic_id')
    candidates: list[Any] = []
    if direct is not None:
        candidates.append(direct)
    topic_envs = telegram.get('topic_id_envs') or [
        'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID',
        'ADMIN_NOTIFICATIONS_TOPIC_ID',
    ]
    if isinstance(topic_envs, str):
        topic_envs = [topic_envs]
    for key in topic_envs:
        candidates.append(env.get(str(key)))
    for value in candidates:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def load_state(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        return {'schema_version': 1, 'checks': {}}
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'schema_version': 1, 'checks': {}}
    if not isinstance(raw, dict):
        return {'schema_version': 1, 'checks': {}}
    raw.setdefault('schema_version', 1)
    raw.setdefault('checks', {})
    if not isinstance(raw['checks'], dict):
        raw['checks'] = {}
    return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    state['updated_at'] = utc_now_iso()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def is_due(check: dict[str, Any], state: dict[str, Any], now: str, settings: MonitorSettings) -> bool:
    interval = check.get('interval_seconds')
    if interval is None and str(check.get('type')) == 'synthetic-vpn':
        interval = settings.synthetic_interval_seconds
    if not interval:
        return True
    previous = state.get('checks', {}).get(str(check.get('id')), {})
    last_checked_at = previous.get('last_checked_at')
    if not last_checked_at:
        return True
    return iso_to_epoch(now) - iso_to_epoch(str(last_checked_at)) >= int(interval)


def run_check(settings: MonitorSettings, check: dict[str, Any]) -> CheckRun:
    check_id = require_str(check, 'id')
    check_type = require_str(check, 'type')
    timeout_seconds = int(check.get('timeout_seconds') or settings.timeout_seconds)
    command = build_command(settings, check, timeout_seconds=timeout_seconds)
    checked_at = utc_now_iso()
    try:
        completed = subprocess.run(
            command,
            cwd=settings.repo_dir,
            text=True,
            capture_output=True,
            timeout=timeout_seconds + 20,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        stdout = ''
        stderr = str(exc)
        returncode = 255
    result = parse_check_result(check, returncode=returncode, stdout=stdout, stderr=stderr)
    return CheckRun(
        check_id=check_id,
        check_type=check_type,
        command=tuple(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        result=result,
        checked_at=checked_at,
    )


def build_command(settings: MonitorSettings, check: dict[str, Any], *, timeout_seconds: int) -> list[str]:
    check_type = require_str(check, 'type')
    config = require_str(check, 'config')
    base = [settings.python, str(settings.node_cli)]
    if check_type == 'availability':
        return base + [
            'availability-check',
            config,
            '--format',
            'json',
            '--timeout-seconds',
            str(timeout_seconds),
        ]
    if check_type == 'ru-edge':
        return base + [
            'ru-edge-check',
            config,
            '--ru-edge-host',
            require_str(check, 'ru_edge_host'),
            '--ru-edge-user',
            str(check.get('ru_edge_user') or 'templar'),
            '--ru-edge-ssh-port',
            str(check.get('ru_edge_ssh_port') or 22),
            '--ru-edge-private-key-ref',
            require_str(check, 'private_key_ref'),
            '--secrets-dir',
            require_str(check, 'secrets_dir'),
            '--format',
            'json',
            '--timeout-seconds',
            str(timeout_seconds),
        ]
    if check_type == 'synthetic-vpn':
        command = base + [
            'synthetic-vpn-check',
            config,
            '--client-config',
            require_str(check, 'client_config'),
            '--xray-bin',
            str(check.get('xray_bin') or '/usr/local/bin/xray'),
            '--format',
            'json',
            '--timeout-seconds',
            str(timeout_seconds),
        ]
        if bool(check.get('expect_warp', False)):
            command.append('--expect-warp')
        else:
            command.append('--no-expect-warp')
        if check.get('local_socks_port'):
            command.extend(['--local-socks-port', str(check['local_socks_port'])])
        if check.get('probe_url'):
            command.extend(['--probe-url', str(check['probe_url'])])
        return command
    raise MonitorError(f'unsupported check type for {check.get("id")}: {check_type}')


def parse_check_result(check: dict[str, Any], *, returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    parsed = parse_json_object(stdout)
    if isinstance(parsed, dict):
        parsed.setdefault('internal_name', check.get('id'))
        parsed.setdefault('vantage', check.get('type'))
        parsed.setdefault('status', 'OK' if parsed.get('ok') else 'UNKNOWN_DEGRADED')
        parsed.setdefault('checks', [])
        return parsed

    message = (stderr or stdout or f'exit code {returncode}').strip()
    if len(message) > 2000:
        message = message[:2000] + '...'
    return {
        'internal_name': check.get('id'),
        'vantage': check.get('type'),
        'status': 'COMMAND_FAIL',
        'ok': False,
        'checks': [
            {
                'name': 'command',
                'status': 'COMMAND_FAIL',
                'ok': False,
                'message': message,
            }
        ],
    }


def parse_json_object(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find('{')
    end = stripped.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def build_notification(
    run: CheckRun,
    state: dict[str, Any],
    settings: MonitorSettings,
    *,
    notify_first_ok: bool,
) -> str | None:
    previous = state.get('checks', {}).get(run.check_id, {})
    previous_ok = previous.get('last_ok')
    now_epoch = iso_to_epoch(run.checked_at)

    if run.ok:
        if previous_ok is False:
            return format_notification('recovered', run, previous)
        if notify_first_ok and previous_ok is None:
            return format_notification('ok', run, previous)
        return None

    last_notified_at = previous.get('last_notified_at')
    if previous_ok is not False:
        return format_notification('failed', run, previous)
    if not last_notified_at:
        return format_notification('failed', run, previous)
    if now_epoch - iso_to_epoch(str(last_notified_at)) >= settings.notify_cooldown_seconds:
        return format_notification('still_failed', run, previous)
    return None


def update_state(state: dict[str, Any], run: CheckRun, notified: bool) -> None:
    checks = state.setdefault('checks', {})
    previous = checks.get(run.check_id, {})
    if not isinstance(previous, dict):
        previous = {}
    record = dict(previous)
    if run.ok:
        record['last_success_at'] = run.checked_at
        if previous.get('last_ok') is False:
            record['recovered_at'] = run.checked_at
        record.pop('failure_started_at', None)
    else:
        if previous.get('last_ok') is not False or not previous.get('failure_started_at'):
            record['failure_started_at'] = run.checked_at
    record.update(
        {
            'id': run.check_id,
            'type': run.check_type,
            'title': run.title,
            'vantage': run.vantage,
            'last_ok': run.ok,
            'last_status': run.status,
            'last_checked_at': run.checked_at,
            'last_returncode': run.returncode,
            'last_result': run.result,
        }
    )
    if notified:
        record['last_notified_at'] = run.checked_at
    checks[run.check_id] = record


def format_notification(kind: str, run: CheckRun, previous: dict[str, Any]) -> str:
    if kind == 'recovered':
        icon = '🟢'
        title = 'Нода восстановилась'
    elif kind == 'still_failed':
        icon = '🟠'
        title = 'Нода всё ещё недоступна'
    elif kind == 'ok':
        icon = '✅'
        title = 'Нода доступна'
    else:
        icon = '🔴'
        title = 'Нода недоступна'

    lines = [
        f'{icon} <b>{title}</b>',
        '',
        f'<b>Проверка:</b> <code>{html.escape(run.check_id)}</code>',
        f'<b>Нода:</b> <code>{html.escape(run.title)}</code>',
        f'<b>Тип:</b> <code>{html.escape(run.check_type)}</code>',
        f'<b>Vantage:</b> <code>{html.escape(run.vantage)}</code>',
        f'<b>Статус:</b> <code>{html.escape(run.status)}</code>',
    ]
    if previous.get('last_status') and previous.get('last_status') != run.status:
        lines.append(f'<b>Было:</b> <code>{html.escape(str(previous["last_status"]))}</code>')
    if previous.get('failure_started_at') and kind in {'still_failed', 'recovered'}:
        lines.append(f'<b>Падение с:</b> <code>{html.escape(str(previous["failure_started_at"]))}</code>')
    lines.append(f'<b>Время:</b> <code>{html.escape(run.checked_at)}</code>')

    details = format_check_details(run.result)
    if details:
        lines.extend(['', '<b>Детали:</b>', f'<pre>{html.escape(details)}</pre>'])
    text = '\n'.join(lines)
    if len(text) > 3900:
        text = text[:3800] + '\n<pre>...details truncated...</pre>'
    return text


def format_check_details(result: dict[str, Any]) -> str:
    rows: list[str] = []
    checks = result.get('checks') or []
    if not isinstance(checks, list):
        return ''
    for item in checks:
        if not isinstance(item, dict):
            continue
        marker = 'OK' if item.get('ok') else 'FAIL'
        name = item.get('name') or 'check'
        status = item.get('status') or 'UNKNOWN'
        message = str(item.get('message') or '').strip()
        rows.append(f'{marker} {name}: {status} - {message}')
    return '\n'.join(rows)


def send_telegram_message(settings: MonitorSettings, text: str) -> bool:
    if not settings.bot_token or not settings.chat_id:
        return False
    payload: dict[str, Any] = {
        'chat_id': settings.chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }
    if settings.topic_id:
        payload['message_thread_id'] = str(settings.topic_id)
    data = urllib.parse.urlencode(payload).encode('utf-8')
    url = f'{TELEGRAM_API_BASE}/bot{settings.bot_token}/sendMessage'
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as response:
            body = response.read(4096).decode('utf-8', errors='replace')
            if response.status >= 300:
                print(f'Telegram API HTTP {response.status}: {body}', file=sys.stderr)
                return False
            parsed = json.loads(body)
            return bool(parsed.get('ok'))
    except Exception as exc:
        print(f'Telegram notify failed: {exc}', file=sys.stderr)
        return False


def require_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if value is None or str(value).strip() == '':
        raise MonitorError(f'check {mapping.get("id", "<unknown>")} requires {key}')
    return str(value)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def iso_to_epoch(value: str) -> float:
    try:
        normalized = value.replace('Z', '+00:00')
        return datetime.fromisoformat(normalized).timestamp()
    except (TypeError, ValueError):
        return 0.0


if __name__ == '__main__':
    raise SystemExit(main())
