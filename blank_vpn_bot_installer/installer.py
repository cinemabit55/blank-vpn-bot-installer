#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from blank_vpn_bot_installer.banner_pack import install_default_banners
    from blank_vpn_bot_installer.templates import caddyfile, landing_html
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blank_vpn_bot_installer.banner_pack import install_default_banners
    from blank_vpn_bot_installer.templates import caddyfile, landing_html


DEFAULT_SOURCE_REF = "main"
BUNDLED_BOT_SOURCE_DIR = Path(__file__).resolve().parents[1] / "bot-source"
DEFAULT_BOT_DIR = Path("/opt/bedolaga")
DEFAULT_REMNAWAVE_DIR = Path("/opt/remnawave")
DEFAULT_CADDY_DIR = Path("/opt/caddy-remnawave")
DEFAULT_CABINET_DIR = Path("/opt/cabinet")
INSTALLER_COMMAND_LIB_DIR = Path("/usr/local/lib/blank-vpn-bot-installer")
INSTALLER_COMMAND_BIN_DIR = Path("/usr/local/bin")
STATE_PATH = Path("/opt/blank-vpn-bot-installer/state.json")
ANSWERS_LAST_PATH = Path("/opt/blank-vpn-bot-installer/answers.last.json")
SUMMARY_PATH = Path("/opt/blank-vpn-bot-installer/install-summary.txt")
CADDY_IMAGE = "caddy:2.10.2"


@dataclass
class InstallerContext:
    dry_run: bool = False
    skip_packages: bool = False
    skip_docker_up: bool = False
    skip_validation: bool = False
    answers_file: Path | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    previous_answers: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    source_mode: str | None = None
    source_repo: str = ""
    source_ref: str = DEFAULT_SOURCE_REF


def status(message: str) -> None:
    print(f"[status] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[warn] {message}", flush=True)


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    printable = " ".join(args)
    if cwd:
        printable = f"(cd {cwd} && {printable})"
    status(printable)
    if dry_run:
        return subprocess.CompletedProcess(args, 0, "", "")
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def run_with_retries(
    args: list[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    attempts: int = 4,
    delay_seconds: int = 15,
) -> subprocess.CompletedProcess[str]:
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return run(args, cwd=cwd, dry_run=dry_run)
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            warn(
                f"Command failed with exit code {exc.returncode}; "
                f"waiting {delay_seconds}s before retry {attempt + 1}/{attempts}"
            )
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def mark(ctx: InstallerContext, key: str, value: Any = True) -> None:
    ctx.state[key] = value
    if not ctx.dry_run:
        save_json(STATE_PATH, ctx.state)


def random_secret(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def random_remnawave_admin_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    rest = [secrets.choice(alphabet) for _ in range(max(length, 24) - len(required))]
    chars = required + rest
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def validate_remnawave_admin_password(password: str) -> None:
    if len(password) < 24:
        die("RemnaWave admin password must be at least 24 characters long")
    if not any(char.isupper() for char in password):
        die("RemnaWave admin password must contain an uppercase letter")
    if not any(char.islower() for char in password):
        die("RemnaWave admin password must contain a lowercase letter")
    if not any(char.isdigit() for char in password):
        die("RemnaWave admin password must contain a digit")


def random_hex(bytes_len: int = 32) -> str:
    return secrets.token_hex(bytes_len)


def preserved_or_generated(ctx: InstallerContext, key: str, factory) -> str:
    current = ctx.answers.get(key)
    if current:
        return str(current)
    previous = ctx.previous_answers.get(key)
    if previous:
        status(f"preserving generated value: {key}")
        return str(previous)
    return str(factory())


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.strip("/")
    if not value:
        raise ValueError("Domain is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", value):
        raise ValueError(f"Invalid domain: {value}")
    return value


def normalize_support(value: str) -> str:
    value = value.strip()
    if not value:
        return "@support"
    if value.startswith(("http://", "https://", "tg://", "@")):
        return value
    if value.startswith(("t.me/", "telegram.me/", "telegram.dog/")):
        return value
    if "." in value:
        return value
    return f"@{value}"


def prompt(ctx: InstallerContext, key: str, label: str, default: str = "", *, secret: bool = False) -> str:
    if key in ctx.answers:
        value = str(ctx.answers[key])
        shown = "***" if secret and value else value
        status(f"{label}: {shown}")
        return value
    suffix = f" [{default}]" if default else ""
    if secret:
        value = getpass.getpass(f"{label}{suffix}: ").strip()
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_domain(ctx: InstallerContext, key: str, label: str, default: str = "") -> str:
    while True:
        try:
            return normalize_domain(prompt(ctx, key, label, default))
        except ValueError as exc:
            if key in ctx.answers:
                raise
            print(f"{exc}. Enter a domain such as example.com, without https://.")


def choice(ctx: InstallerContext, key: str, label: str, options: list[tuple[str, str]], default: str) -> str:
    if key in ctx.answers:
        value = str(ctx.answers[key])
        status(f"{label}: {value}")
        return value
    print(label)
    for idx, (value, text) in enumerate(options, start=1):
        default_mark = " [default]" if value == default else ""
        print(f"{idx}. {text}{default_mark}")
    while True:
        raw = input(f"Choose [default {default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        allowed = {value for value, _ in options}
        if raw in allowed:
            return raw
        print("Invalid choice.")


def yes_no(ctx: InstallerContext, key: str, label: str, default: bool) -> bool:
    if key in ctx.answers:
        return bool(ctx.answers[key])
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true", "да", "д", "у"}:
            return True
        if raw in {"n", "no", "0", "false", "нет", "н"}:
            return False
        print("Enter y/yes or n/no.")


def detect_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.read().decode("utf-8").strip()
        except Exception:
            continue
    return ""


def ensure_root(*, dry_run: bool = False) -> None:
    if dry_run:
        return
    if os.geteuid() != 0:
        die("Run as root: sudo python3 blank_vpn_bot_installer/installer.py")


def preflight(ctx: InstallerContext) -> None:
    status("preflight checks")
    ensure_root(dry_run=ctx.dry_run)
    required = ["git", "docker"]
    missing = [cmd for cmd in required if shutil.which(cmd) is None]
    if missing:
        if ctx.dry_run:
            warn(f"dry-run: missing required command(s): {', '.join(missing)}")
            return
        die(f"Missing required command(s): {', '.join(missing)}")
    compose_ok = run(["docker", "compose", "version"], dry_run=ctx.dry_run, check=False, capture=True)
    if not ctx.dry_run and compose_ok.returncode != 0:
        die("Docker Compose plugin is required")
    if not ctx.dry_run:
        node_markers = [path for path in (Path("/opt/remnanode"), Path("/opt/templar-node")) if path.exists()]
        if node_markers:
            die(
                "This server already contains a VPN node "
                f"({', '.join(str(path) for path in node_markers)}). "
                "Install the bot control plane on a separate clean VPS."
            )
    busy_ports: list[int] = []
    for port in (80, 443):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                busy_ports.append(port)
    if busy_ports:
        ports = ", ".join(str(port) for port in busy_ports)
        if not ctx.dry_run and docker_container_ready("caddy-remnawave"):
            warn(f"Port(s) {ports} are already used by the current Caddy container.")
        elif ctx.dry_run:
            warn(f"Port(s) {ports} are already in use.")
        else:
            die(
                f"Port(s) {ports} are already in use by another service. "
                "The bot control plane requires a clean VPS with ports 80 and 443 available."
            )


def collect_answers(ctx: InstallerContext) -> dict[str, Any]:
    status("collecting install answers")
    source_mode = str(ctx.answers.get("source_mode") or ctx.source_mode or "bundled")
    if source_mode not in {"bundled", "git"}:
        die("source_mode must be bundled or git")
    source_repo = str(ctx.answers.get("source_repo") or ctx.source_repo or "")
    source_ref = str(ctx.answers.get("source_ref") or ctx.source_ref or DEFAULT_SOURCE_REF)
    status(
        "Bot source: bundled bot-source from this installer repository"
        if source_mode == "bundled"
        else f"Bot source: external Git repository ({source_repo or 'prompt required'})"
    )
    if source_mode == "git":
        if not source_repo:
            source_repo = prompt(ctx, "source_repo", "Bot source repository URL")
        if "source_ref" not in ctx.answers and not ctx.source_ref:
            source_ref = prompt(ctx, "source_ref", "Bot source ref/branch/tag", DEFAULT_SOURCE_REF)
    install_root = Path(prompt(ctx, "install_root", "Install root", "/opt"))
    project_name = prompt(ctx, "project_name", "Project display name", "VPN Service")

    public_ip = prompt(ctx, "server_ip", "Server public IPv4", detect_public_ip())
    root_domain = prompt_domain(ctx, "root_domain", "Root domain, e.g. example.com")
    panel_domain = prompt_domain(ctx, "panel_domain", "Panel domain", f"panel.{root_domain}")
    sub_domain = prompt_domain(ctx, "sub_domain", "Subscription domain", f"sub.{root_domain}")
    cabinet_domain = prompt_domain(ctx, "cabinet_domain", "Cabinet domain", f"cabinet.{root_domain}")
    api_domain = prompt_domain(ctx, "api_domain", "API/webhook domain", f"api.{root_domain}")
    le_email = prompt(ctx, "le_email", "Let's Encrypt email", f"admin@{root_domain}")

    dns_mode = choice(
        ctx,
        "dns_mode",
        "DNS setup",
        [
            ("manual", "manual records, installer only prints them"),
            ("cloudflare", "Cloudflare API token, installer upserts A records"),
        ],
        "manual",
    )
    cf_token = ""
    cf_proxy_public = False
    if dns_mode == "cloudflare":
        cf_token = prompt(ctx, "cloudflare_token", "Cloudflare API token", secret=True)
        cf_proxy_public = yes_no(ctx, "cloudflare_proxy_public", "Proxy panel/cabinet/api through Cloudflare", False)

    bot_token = prompt(ctx, "bot_token", "Telegram bot token", secret=True)
    bot_username = prompt(ctx, "bot_username", "Telegram bot username without @", "")
    bot_username = bot_username.lstrip("@")
    admin_ids = prompt(ctx, "admin_ids", "Telegram admin IDs, comma-separated", "")
    support_username = normalize_support(prompt(ctx, "support_username", "Telegram support", "@support"))
    support_mode = choice(
        ctx,
        "support_mode",
        "Support mode",
        [
            ("both", "tickets + contact"),
            ("tickets", "tickets only"),
            ("contact", "contact only"),
        ],
        "both",
    )

    stars_rate = prompt(ctx, "telegram_stars_rate_rub", "Telegram Stars RUB rate", "1.3")
    remnawave_admin_username = prompt(ctx, "remnawave_admin_username", "RemnaWave admin username", "admin")
    previous_admin_password = str(ctx.previous_answers.get("remnawave_admin_password") or "")
    remnawave_admin_password = prompt(
        ctx,
        "remnawave_admin_password",
        "RemnaWave admin password (empty = keep existing/generate)",
        "",
        secret=True,
    )
    if not remnawave_admin_password:
        remnawave_admin_password = previous_admin_password or random_remnawave_admin_password()
    validate_remnawave_admin_password(remnawave_admin_password)
    remnawave_api_key = prompt(
        ctx,
        "remnawave_api_key",
        "Existing RemnaWave API token (empty = installer creates it)",
        "",
        secret=True,
    )
    if not remnawave_api_key and ctx.previous_answers.get("remnawave_api_key"):
        status("preserving existing RemnaWave API token")
        remnawave_api_key = str(ctx.previous_answers["remnawave_api_key"])

    return {
        "source_mode": source_mode,
        "source_repo": source_repo,
        "source_ref": source_ref,
        "bot_dir": str(install_root / "bedolaga"),
        "remnawave_dir": str(install_root / "remnawave"),
        "caddy_dir": str(install_root / "caddy-remnawave"),
        "cabinet_dir": str(install_root / "cabinet"),
        "project_name": project_name,
        "server_ip": public_ip,
        "root_domain": root_domain,
        "panel_domain": panel_domain,
        "sub_domain": sub_domain,
        "cabinet_domain": cabinet_domain,
        "api_domain": api_domain,
        "le_email": le_email,
        "dns_mode": dns_mode,
        "cloudflare_token": cf_token,
        "cloudflare_proxy_public": cf_proxy_public,
        "bot_token": bot_token,
        "bot_username": bot_username,
        "admin_ids": admin_ids,
        "support_username": support_username,
        "support_mode": support_mode,
        "telegram_stars_rate_rub": stars_rate,
        "remnawave_admin_username": remnawave_admin_username,
        "remnawave_admin_password": remnawave_admin_password,
        "remnawave_api_key": remnawave_api_key,
        "postgres_password": preserved_or_generated(ctx, "postgres_password", lambda: random_secret(32)),
        "remnawave_postgres_password": preserved_or_generated(ctx, "remnawave_postgres_password", lambda: random_secret(32)),
        "web_api_token": preserved_or_generated(ctx, "web_api_token", lambda: random_secret(48)),
        "web_api_hmac": preserved_or_generated(ctx, "web_api_hmac", lambda: random_secret(64)),
        "cabinet_jwt_secret": preserved_or_generated(ctx, "cabinet_jwt_secret", lambda: random_secret(64)),
        "remnawave_jwt_secret": preserved_or_generated(ctx, "remnawave_jwt_secret", lambda: random_hex(64)),
        "remnawave_api_tokens_secret": preserved_or_generated(ctx, "remnawave_api_tokens_secret", lambda: random_hex(64)),
        "remnawave_webhook_secret": preserved_or_generated(ctx, "remnawave_webhook_secret", lambda: random_secret(64)),
        "metrics_user": preserved_or_generated(ctx, "metrics_user", lambda: f"metrics_{secrets.token_hex(4)}"),
        "metrics_pass": preserved_or_generated(ctx, "metrics_pass", lambda: random_secret(24)),
    }


def prepare_bot_source(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    if cfg["source_mode"] == "bundled":
        install_bundled_bot_source(ctx, cfg)
        return
    git_clone_or_update(ctx, cfg)


def install_bundled_bot_source(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_dir = Path(cfg["bot_dir"])
    status(f"using bundled bot source: {BUNDLED_BOT_SOURCE_DIR}")
    if not ctx.dry_run and not BUNDLED_BOT_SOURCE_DIR.exists():
        die(f"Bundled bot source is missing: {BUNDLED_BOT_SOURCE_DIR}")
    copy_tree(BUNDLED_BOT_SOURCE_DIR, bot_dir, dry_run=ctx.dry_run)
    mark(ctx, "bot_source_ready", {"mode": "bundled"})


def git_clone_or_update(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_dir = Path(cfg["bot_dir"])
    repo = cfg["source_repo"]
    ref = cfg["source_ref"]
    if not repo:
        die("Bot source repository URL is required when source_mode=git")

    status("checking bot source repository access")
    probe = run(["git", "ls-remote", "--heads", "--tags", repo], dry_run=ctx.dry_run, check=False, capture=True)
    if not ctx.dry_run and probe.returncode != 0:
        die(
            "Cannot access bot source repository. Configure SSH deploy key or use an HTTPS URL/token, "
            f"then retry. Repository: {repo}"
        )

    if (bot_dir / ".git").exists():
        run(["git", "fetch", "origin", ref], cwd=bot_dir, dry_run=ctx.dry_run)
        run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=bot_dir, dry_run=ctx.dry_run)
    else:
        bot_dir.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", repo, str(bot_dir)], dry_run=ctx.dry_run)
        run(["git", "checkout", ref], cwd=bot_dir, dry_run=ctx.dry_run)
    mark(ctx, "bot_source_ready", {"mode": "git", "repo": repo, "ref": ref})


def write_file(path: Path, content: str, *, mode: int = 0o644, dry_run: bool = False) -> None:
    status(f"write {path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def copy_file(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    status(f"copy {src} -> {dst}")
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path, *, dry_run: bool = False) -> None:
    status(f"sync {src} -> {dst}")
    if dry_run:
        return
    shutil.copytree(src, dst, dirs_exist_ok=True)


def bot_env(cfg: dict[str, Any]) -> str:
    api_key = cfg["remnawave_api_key"] or "FILL_REMNAWAVE_API_TOKEN_LATER"
    return "\n".join(
        [
            f"BOT_TOKEN={cfg['bot_token']}",
            f"BOT_USERNAME={cfg['bot_username']}",
            f"ADMIN_IDS={cfg['admin_ids']}",
            "ADMIN_EMAILS=",
            f"SUPPORT_USERNAME={cfg['support_username']}",
            "SUPPORT_MENU_ENABLED=true",
            f"SUPPORT_SYSTEM_MODE={cfg['support_mode']}",
            "MINIAPP_TICKETS_ENABLED=true",
            "",
            "DATABASE_MODE=postgresql",
            "POSTGRES_HOST=postgres",
            "POSTGRES_PORT=5432",
            "POSTGRES_DB=remnawave_bot",
            "POSTGRES_USER=remnawave_user",
            f"POSTGRES_PASSWORD={cfg['postgres_password']}",
            "REDIS_URL=redis://redis:6379/0",
            "",
            "REMNAWAVE_API_URL=http://remnawave:3000",
            f"REMNAWAVE_API_KEY={api_key}",
            "REMNAWAVE_AUTH_TYPE=api_key",
            "REMNAWAVE_USER_DELETE_MODE=delete",
            "REMNAWAVE_AUTO_SYNC_ENABLED=false",
            "REMNAWAVE_AUTO_SYNC_TIMES=03:00",
            f"REMNAWAVE_WEBHOOK_SECRET={cfg['remnawave_webhook_secret']}",
            "REMNAWAVE_WEBHOOK_ENABLED=true",
            "REMNAWAVE_WEBHOOK_PATH=/remnawave-webhook",
            "",
            "WEB_API_ENABLED=true",
            "WEB_API_HOST=0.0.0.0",
            "WEB_API_PORT=8080",
            f"WEB_API_DEFAULT_TOKEN={cfg['web_api_token']}",
            f"WEB_API_TOKEN_HMAC_SECRET={cfg['web_api_hmac']}",
            "WEB_API_DOCS_ENABLED=false",
            f"WEBHOOK_URL=https://{cfg['api_domain']}",
            "",
            "CABINET_ENABLED=true",
            f"CABINET_URL=https://{cfg['cabinet_domain']}",
            f"CABINET_ALLOWED_ORIGINS=https://{cfg['cabinet_domain']}",
            f"CABINET_JWT_SECRET={cfg['cabinet_jwt_secret']}",
            "CABINET_EMAIL_VERIFICATION_ENABLED=false",
            "CABINET_EMAIL_AUTH_ENABLED=true",
            "",
            "SALES_MODE=tariffs",
            "MULTI_TARIFF_ENABLED=true",
            "MAX_ACTIVE_SUBSCRIPTIONS=10",
            "DEFAULT_TARIFF_BOOTSTRAP_ENABLED=true",
            "DEFAULT_TARIFF_BASIC_NAME=Базовый",
            "DEFAULT_TARIFF_DARK_NAME=Темные списки",
            "DEFAULT_TARIFF_TRIAL_NAME=Триал",
            "TRIAL_DURATION_DAYS=3",
            "TRIAL_TRAFFIC_LIMIT_GB=10",
            "TRIAL_DEVICE_LIMIT=1",
            "TRIAL_PAYMENT_ENABLED=false",
            "TRIAL_ACTIVATION_PRICE=0",
            "",
            "TELEGRAM_STARS_ENABLED=true",
            f"TELEGRAM_STARS_RATE_RUB={cfg['telegram_stars_rate_rub']}",
            "TELEGRAM_STARS_DISPLAY_NAME=Telegram Stars",
            "YOOKASSA_ENABLED=false",
            "CRYPTOBOT_ENABLED=false",
            "HELEKET_ENABLED=false",
            "MULENPAY_ENABLED=false",
            "PAL24_ENABLED=false",
            "PLATEGA_ENABLED=false",
            "WATA_ENABLED=false",
            "CLOUDPAYMENTS_ENABLED=false",
            "FREEKASSA_ENABLED=false",
            "KASSA_AI_ENABLED=false",
            "RIOPAY_ENABLED=false",
            "SEVERPAY_ENABLED=false",
            "PAYPEAR_ENABLED=false",
            "ROLLYPAY_ENABLED=false",
            "OVERPAY_ENABLED=false",
            "AURAPAY_ENABLED=false",
            "",
            "LOG_LEVEL=INFO",
            "LOG_ROTATION_ENABLED=true",
            "TZ=Europe/Moscow",
            "",
        ]
    )


def remnawave_env(cfg: dict[str, Any]) -> str:
    return "\n".join(
        [
            "APP_PORT=3000",
            "METRICS_PORT=3001",
            "API_INSTANCES=1",
            'DATABASE_URL="postgresql://postgres:{password}@remnawave-db:5432/postgres"'.format(
                password=cfg["remnawave_postgres_password"]
            ),
            "REDIS_SOCKET=/var/run/valkey/valkey.sock",
            f"JWT_AUTH_SECRET={cfg['remnawave_jwt_secret']}",
            f"JWT_API_TOKENS_SECRET={cfg['remnawave_api_tokens_secret']}",
            "IS_TELEGRAM_NOTIFICATIONS_ENABLED=false",
            "TELEGRAM_BOT_TOKEN=",
            'TELEGRAM_NOTIFY_USERS=""',
            'TELEGRAM_NOTIFY_NODES=""',
            'TELEGRAM_NOTIFY_CRM=""',
            'TELEGRAM_NOTIFY_SERVICE=""',
            'TELEGRAM_NOTIFY_TBLOCKER=""',
            f"PANEL_DOMAIN={cfg['panel_domain']}",
            f"FRONT_END_DOMAIN={cfg['panel_domain']}",
            f"SUB_PUBLIC_DOMAIN={cfg['sub_domain']}/connect",
            "SWAGGER_PATH=/docs",
            "SCALAR_PATH=/scalar",
            "IS_DOCS_ENABLED=false",
            f"METRICS_USER={cfg['metrics_user']}",
            f"METRICS_PASS={cfg['metrics_pass']}",
            "WEBHOOK_ENABLED=true",
            f"WEBHOOK_URL=https://{cfg['api_domain']}/remnawave-webhook",
            f"WEBHOOK_SECRET_HEADER={cfg['remnawave_webhook_secret']}",
            "BANDWIDTH_USAGE_NOTIFICATIONS_ENABLED=false",
            "NOT_CONNECTED_USERS_NOTIFICATIONS_ENABLED=false",
            "POSTGRES_USER=postgres",
            f"POSTGRES_PASSWORD={cfg['remnawave_postgres_password']}",
            "POSTGRES_DB=postgres",
            "",
        ]
    )


def remnawave_subscription_env(cfg: dict[str, Any]) -> str:
    api_token = cfg["remnawave_api_key"] or "FILL_REMNAWAVE_API_TOKEN_LATER"
    return "\n".join(
        [
            "REMNAWAVE_PANEL_URL=http://remnawave:3000",
            "APP_PORT=3010",
            "CUSTOM_SUB_PREFIX=connect",
            f"REMNAWAVE_API_TOKEN={api_token}",
            "",
        ]
    )


def cabinet_env(cfg: dict[str, Any]) -> str:
    return "\n".join(
        [
            "VITE_API_URL=/api",
            f"VITE_TELEGRAM_BOT_USERNAME={cfg['bot_username']}",
            f"VITE_APP_NAME={cfg['project_name']}",
            "VITE_APP_LOGO=V",
            "CABINET_PORT=3020",
            "",
        ]
    )


def caddy_env(cfg: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"PANEL_DOMAIN={cfg['panel_domain']}",
            f"SUB_DOMAIN={cfg['sub_domain']}",
            f"CABINET_DOMAIN={cfg['cabinet_domain']}",
            f"API_DOMAIN={cfg['api_domain']}",
            "PANEL_PORT=3000",
            "SUB_PORT=3010",
            "SUB_PREFIX=connect",
            "",
        ]
    )


def write_configs(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_dir = Path(cfg["bot_dir"])
    remnawave_dir = Path(cfg["remnawave_dir"])
    caddy_dir = Path(cfg["caddy_dir"])
    cabinet_dir = Path(cfg["cabinet_dir"])

    source_ops = bot_dir / "ops"
    copy_file(source_ops / "remnawave" / "docker-compose.yml", remnawave_dir / "docker-compose.yml", dry_run=ctx.dry_run)
    copy_tree(source_ops / "cabinet", cabinet_dir, dry_run=ctx.dry_run)

    # Use a generated Caddyfile without checked-in origin certificate paths.
    copy_file(source_ops / "caddy-remnawave" / "docker-compose.yml", caddy_dir / "docker-compose.yml", dry_run=ctx.dry_run)
    write_file(caddy_dir / "Caddyfile", caddyfile(
        cfg["panel_domain"], cfg["sub_domain"], cfg["cabinet_domain"], cfg["api_domain"], cfg["le_email"]
    ), dry_run=ctx.dry_run)
    write_file(caddy_dir / "landing" / "index.html", landing_html(cfg["project_name"]), dry_run=ctx.dry_run)

    write_file(bot_dir / ".env", bot_env(cfg), mode=0o600, dry_run=ctx.dry_run)
    write_file(remnawave_dir / ".env", remnawave_env(cfg), mode=0o600, dry_run=ctx.dry_run)
    write_file(remnawave_dir / ".env.subscription", remnawave_subscription_env(cfg), mode=0o600, dry_run=ctx.dry_run)
    write_file(cabinet_dir / ".env", cabinet_env(cfg), mode=0o600, dry_run=ctx.dry_run)
    write_file(caddy_dir / ".env", caddy_env(cfg), mode=0o600, dry_run=ctx.dry_run)
    mark(ctx, "configs_written", True)


def install_default_banner_pack(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    count = install_default_banners(
        Path(cfg["bot_dir"]),
        cfg["project_name"],
        dry_run=ctx.dry_run,
        status=status,
        warn=warn,
    )
    mark(ctx, "default_banners_installed", {"count": count})


def prepare_bot_runtime_dirs(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_dir = Path(cfg["bot_dir"])
    runtime_dirs = [bot_dir / name for name in ("data", "logs", "uploads", "locales")]
    status("preparing writable bot runtime directories")
    if ctx.dry_run:
        return
    for path in runtime_dirs:
        path.mkdir(parents=True, exist_ok=True)
    run(["chown", "-R", "1000:1000", *(str(path) for path in runtime_dirs)])


def patch_caddy_compose(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    path = Path(cfg["caddy_dir"]) / "docker-compose.yml"
    if ctx.dry_run:
        return
    raw = path.read_text(encoding="utf-8")
    # Generated Caddyfile uses ACME storage, not mounted origin certificates.
    raw = raw.replace("      - ./certs:/etc/caddy/certs:ro\n", "")
    path.write_text(raw, encoding="utf-8")


def validate_generated_configs(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    if ctx.dry_run:
        status("dry-run: would validate docker compose and Caddy config")
        return
    if ctx.skip_validation:
        warn("Skipping generated config validation")
        return

    status("validating generated docker compose configs")
    compose_dirs = [
        Path(cfg["remnawave_dir"]),
        Path(cfg["bot_dir"]),
        Path(cfg["cabinet_dir"]),
        Path(cfg["caddy_dir"]),
    ]
    for compose_dir in compose_dirs:
        compose_file = compose_dir / "docker-compose.yml"
        if compose_file.exists():
            run(["docker", "compose", "config", "-q"], cwd=compose_dir)
        else:
            warn(f"Compose file not found, skipping validation: {compose_file}")

    status("validating generated Caddyfile")
    caddy_dir = Path(cfg["caddy_dir"])
    (caddy_dir / "logs").mkdir(parents=True, exist_ok=True)
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{caddy_dir / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            "-v",
            f"{caddy_dir / 'landing'}:/srv/landing:ro",
            "-v",
            f"{caddy_dir / 'logs'}:/var/log/caddy",
            CADDY_IMAGE,
            "caddy",
            "validate",
            "--config",
            "/etc/caddy/Caddyfile",
        ]
    )
    mark(ctx, "generated_configs_validated", True)


def open_firewall(ctx: InstallerContext) -> None:
    if shutil.which("ufw") is None:
        return
    run(["ufw", "allow", "80/tcp"], dry_run=ctx.dry_run, check=False)
    run(["ufw", "allow", "443/tcp"], dry_run=ctx.dry_run, check=False)
    run(["ufw", "allow", "443/udp"], dry_run=ctx.dry_run, check=False)


def docker_up_remnawave(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    if ctx.skip_docker_up:
        warn("Skipping docker compose up")
        return
    remnawave_dir = Path(cfg["remnawave_dir"])

    run_with_retries(["docker", "compose", "up", "-d", "remnawave"], cwd=remnawave_dir, dry_run=ctx.dry_run)
    mark(ctx, "remnawave_docker_up", True)


def remnawave_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    bearer: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
        "X-Remnawave-Client-Type": "browser",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        f"http://127.0.0.1:3000{path}",
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def response_value(response: dict[str, Any], *path: str) -> Any:
    current: Any = response
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def docker_container_health(container_name: str) -> str:
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container_name,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return "missing"
    if result.returncode != 0:
        return "missing"
    return result.stdout.strip() or "unknown"


def docker_container_ready(container_name: str) -> bool:
    return docker_container_health(container_name) in {"healthy", "running"}


def wait_for_remnawave_api(
    ctx: InstallerContext,
    *,
    remnawave_dir: Path = DEFAULT_REMNAWAVE_DIR,
) -> dict[str, Any] | None:
    if ctx.dry_run or ctx.skip_docker_up:
        return None
    status("waiting for RemnaWave API")
    last_error = ""
    restarted = False
    for attempt in range(120):
        try:
            return remnawave_request("GET", "/api/auth/status", timeout=5)
        except Exception as exc:
            last_error = str(exc)
            elapsed = (attempt + 1) * 5
            if elapsed % 30 == 0:
                database_health = docker_container_health("remnawave-db")
                backend_health = docker_container_health("remnawave")
                status(
                    f"still waiting for RemnaWave API ({elapsed}s); "
                    f"database={database_health}, backend={backend_health}"
                )
                if elapsed >= 90 and not restarted and database_health == "healthy" and backend_health != "healthy":
                    warn("RemnaWave database is healthy but backend API is not ready; restarting backend once")
                    run(["docker", "compose", "restart", "remnawave"], cwd=remnawave_dir)
                    restarted = True
            time.sleep(5)
    die(f"RemnaWave API did not become ready within 10 minutes: {last_error}")


def remnawave_auth_jwt(cfg: dict[str, Any], status_response: dict[str, Any] | None) -> str:
    username = cfg["remnawave_admin_username"]
    password = cfg["remnawave_admin_password"]
    is_register_allowed = bool(response_value(status_response or {}, "response", "isRegisterAllowed"))

    if is_register_allowed:
        status("registering RemnaWave admin")
        try:
            registered = remnawave_request(
                "POST",
                "/api/auth/register",
                payload={"username": username, "password": password},
            )
            access_token = response_value(registered, "response", "accessToken")
            if access_token:
                return str(access_token)
        except urllib.error.HTTPError as exc:
            warn(f"RemnaWave admin registration failed with HTTP {exc.code}; trying login")

    status("logging in to RemnaWave admin")
    logged_in = remnawave_request(
        "POST",
        "/api/auth/login",
        payload={"username": username, "password": password},
    )
    access_token = response_value(logged_in, "response", "accessToken")
    if not access_token:
        die("RemnaWave login did not return accessToken")
    return str(access_token)


def bootstrap_remnawave_api_token(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    if cfg["remnawave_api_key"]:
        status("using existing RemnaWave API token")
        return
    if ctx.dry_run or ctx.skip_docker_up:
        status("dry-run/skip: would create RemnaWave admin and API token")
        return

    status("bootstrapping RemnaWave API token")
    status_response = wait_for_remnawave_api(ctx, remnawave_dir=Path(cfg["remnawave_dir"]))
    admin_jwt = remnawave_auth_jwt(cfg, status_response)
    token_response = remnawave_request(
        "POST",
        "/api/tokens",
        payload={"tokenName": "blank-vpn-bot-installer"},
        bearer=admin_jwt,
    )
    api_token = response_value(token_response, "response", "token")
    if not api_token:
        die("RemnaWave API token creation did not return token")

    cfg["remnawave_api_key"] = str(api_token)
    write_file(Path(cfg["bot_dir"]) / ".env", bot_env(cfg), mode=0o600, dry_run=ctx.dry_run)
    write_file(
        Path(cfg["remnawave_dir"]) / ".env.subscription",
        remnawave_subscription_env(cfg),
        mode=0o600,
        dry_run=ctx.dry_run,
    )
    save_json(ANSWERS_LAST_PATH, cfg)
    mark(ctx, "remnawave_api_token_created", True)


def docker_up_application(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    if ctx.skip_docker_up:
        warn("Skipping application docker compose up")
        return
    bot_dir = Path(cfg["bot_dir"])
    remnawave_dir = Path(cfg["remnawave_dir"])
    cabinet_dir = Path(cfg["cabinet_dir"])
    caddy_dir = Path(cfg["caddy_dir"])

    if not ctx.dry_run and docker_container_ready("remnawave-subscription-page"):
        status("subscription page is already running; skipping image pull")
    else:
        status("pulling subscription page image; interrupted downloads will be retried")
        run_with_retries(
            ["docker", "compose", "pull", "remnawave-subscription-page"],
            cwd=remnawave_dir,
            dry_run=ctx.dry_run,
        )
        run_with_retries(
            ["docker", "compose", "up", "-d", "--pull", "never", "remnawave-subscription-page"],
            cwd=remnawave_dir,
            dry_run=ctx.dry_run,
        )

    bot_ready = all(
        docker_container_ready(name)
        for name in ("remnawave_bot_db", "remnawave_bot_redis", "remnawave_bot")
    )
    if not ctx.dry_run and bot_ready:
        status("bot stack is already running; applying current configuration")
        run_with_retries(
            ["docker", "compose", "up", "-d", "--pull", "never", "bot"],
            cwd=bot_dir,
            dry_run=ctx.dry_run,
        )
    else:
        status("pulling bot database and Redis images; interrupted downloads will be retried")
        run_with_retries(["docker", "compose", "pull", "postgres", "redis"], cwd=bot_dir, dry_run=ctx.dry_run)
        status("building bot image")
        run_with_retries(["docker", "compose", "build", "bot"], cwd=bot_dir, dry_run=ctx.dry_run)
        run_with_retries(["docker", "compose", "up", "-d", "--pull", "never"], cwd=bot_dir, dry_run=ctx.dry_run)

    if not ctx.dry_run and docker_container_ready("cabinet_frontend"):
        status("cabinet is already running; skipping build")
    else:
        status("building cabinet image")
        run_with_retries(["docker", "compose", "build"], cwd=cabinet_dir, dry_run=ctx.dry_run)
        run_with_retries(["docker", "compose", "up", "-d", "--pull", "never"], cwd=cabinet_dir, dry_run=ctx.dry_run)

    if not ctx.dry_run and docker_container_ready("caddy-remnawave"):
        status("Caddy is already running; skipping start")
    else:
        run_with_retries(["docker", "compose", "up", "-d", "--pull", "never"], cwd=caddy_dir, dry_run=ctx.dry_run)
    mark(ctx, "application_docker_up", True)


def install_node_tools_venv(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_dir = Path(cfg["bot_dir"])
    venv_dir = bot_dir / ".venv-templar-node"
    python_bin = venv_dir / "bin" / "python"
    requirements = bot_dir / "requirements.txt"

    status("installing node command Python environment")
    run(["python3", "-m", "venv", str(venv_dir)], dry_run=ctx.dry_run)
    run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], dry_run=ctx.dry_run)
    if requirements.exists() or ctx.dry_run:
        run([str(python_bin), "-m", "pip", "install", "-r", str(requirements)], dry_run=ctx.dry_run)
    else:
        run(
            [
                str(python_bin),
                "-m",
                "pip",
                "install",
                "PyYAML>=6.0",
                "pydantic>=2.0",
                "pydantic-settings>=2.0",
                "python-dotenv>=1.0",
                "SQLAlchemy>=2.0",
                "asyncpg>=0.29",
                "aiohttp>=3.9",
                "aiohttp-socks>=0.10",
                "httpx[socks]>=0.27",
                "redis>=5.0",
                "structlog>=25.1",
                "rich>=14.0",
                "cryptography>=44.0",
                "python-dateutil>=2.9",
                "pytz>=2023.4",
                "packaging>=24.0",
            ],
            dry_run=ctx.dry_run,
        )
    run(
        [
            str(python_bin),
            "-c",
            "import yaml, pydantic, sqlalchemy, asyncpg; from app.templar_node.cli import main; print('node tools ok')",
        ],
        cwd=bot_dir,
        dry_run=ctx.dry_run,
    )
    mark(ctx, "node_tools_venv_installed", str(venv_dir))


def install_aliases(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_dir = Path(cfg["bot_dir"])
    node_installer = bot_dir / "scripts" / "install_templar_node_aliases.sh"
    bot_installer = bot_dir / "scripts" / "install_templar_bot_aliases.sh"
    if node_installer.exists() or ctx.dry_run:
        install_node_tools_venv(ctx, cfg)
        run(["bash", str(node_installer), str(bot_dir)], dry_run=ctx.dry_run)
    else:
        warn(f"Node alias installer not found: {node_installer}")
    if bot_installer.exists() or ctx.dry_run:
        run(["bash", str(bot_installer), str(bot_dir)], dry_run=ctx.dry_run)
        run(["python3", "-m", "venv", str(bot_dir / ".venv-bot-tools")], dry_run=ctx.dry_run, check=False)
        run(
            [str(bot_dir / ".venv-bot-tools" / "bin" / "python"), "-m", "pip", "install", "--upgrade", "pip", "pillow"],
            dry_run=ctx.dry_run,
            check=False,
        )
    else:
        warn(f"Bot alias installer not found: {bot_installer}")
    mark(ctx, "aliases_installed", True)


def install_installer_commands(ctx: InstallerContext) -> None:
    status("installing installer helper commands")
    if ctx.dry_run:
        status(f"write {INSTALLER_COMMAND_BIN_DIR / 'add_payment'}")
        return

    INSTALLER_COMMAND_LIB_DIR.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent / "payment_admin.py"
    target = INSTALLER_COMMAND_LIB_DIR / "payment_admin.py"
    wrapper = INSTALLER_COMMAND_BIN_DIR / "add_payment"
    shutil.copy2(source, target)
    target.chmod(0o755)
    wrapper.write_text(
        "#!/usr/bin/env sh\n"
        f'exec python3 "{target}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    mark(ctx, "installer_commands_installed", True)


def cloudflare_request(token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cloudflare API error {exc.code}: {body}") from exc
    parsed = json.loads(raw)
    if not parsed.get("success"):
        raise RuntimeError(f"Cloudflare API failed: {parsed}")
    return parsed


def setup_dns(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    records = [
        (cfg["panel_domain"], cfg["cloudflare_proxy_public"]),
        (cfg["sub_domain"], False),
        (cfg["cabinet_domain"], cfg["cloudflare_proxy_public"]),
        (cfg["api_domain"], cfg["cloudflare_proxy_public"]),
    ]
    if cfg["dns_mode"] != "cloudflare":
        print("\nManual DNS records to create:")
        for name, proxied in records:
            proxy_note = "DNS only" if not proxied else "proxied allowed"
            print(f"  A {name} -> {cfg['server_ip']} ({proxy_note})")
        print("Important: subscription domain must stay DNS-only, not Cloudflare-proxied.\n")
        return

    if ctx.dry_run:
        status("dry-run: would upsert Cloudflare DNS records")
        return

    token = cfg["cloudflare_token"]
    root_domain = cfg["root_domain"]
    zone_resp = cloudflare_request(token, "GET", f"/zones?name={root_domain}")
    result = zone_resp.get("result") or []
    if not result:
        die(f"Cloudflare zone not found for {root_domain}")
    zone_id = result[0]["id"]

    for name, proxied in records:
        status(f"upsert Cloudflare A {name} -> {cfg['server_ip']}")
        existing = cloudflare_request(token, "GET", f"/zones/{zone_id}/dns_records?type=A&name={name}")
        payload = {
            "type": "A",
            "name": name,
            "content": cfg["server_ip"],
            "ttl": 1,
            "proxied": bool(proxied),
        }
        matches = existing.get("result") or []
        if matches:
            cloudflare_request(token, "PUT", f"/zones/{zone_id}/dns_records/{matches[0]['id']}", payload)
        else:
            cloudflare_request(token, "POST", f"/zones/{zone_id}/dns_records", payload)
    mark(ctx, "dns_done", True)


def bot_health_probe() -> subprocess.CompletedProcess[str] | None:
    probe_code = (
        "import os, urllib.request; "
        "request = urllib.request.Request("
        "'http://127.0.0.1:8080/health', "
        "headers={'X-API-Key': os.environ['WEB_API_DEFAULT_TOKEN']}"
        "); "
        "urllib.request.urlopen(request, timeout=3).read()"
    )
    try:
        return subprocess.run(
            ["docker", "exec", "remnawave_bot", "python", "-c", probe_code],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return None


def wait_for_health(ctx: InstallerContext) -> bool:
    if ctx.dry_run or ctx.skip_docker_up:
        return True
    status("waiting for bot health endpoint")
    for attempt in range(60):
        health = docker_container_health("remnawave_bot")
        probe = bot_health_probe()
        if health == "healthy" or (probe is not None and probe.returncode == 0):
            status("bot health endpoint is ready")
            return True
        if attempt % 6 == 0:
            status(f"bot container health: {health}; still waiting")
        time.sleep(5)
    warn("Bot health endpoint did not become ready within 5 minutes")
    run(["docker", "inspect", "--format", "{{json .State}}", "remnawave_bot"], check=False)
    run(["docker", "logs", "--tail", "80", "remnawave_bot"], check=False)
    return False


def write_summary(ctx: InstallerContext, cfg: dict[str, Any]) -> None:
    bot_source = "bundled bot-source"
    if cfg["source_mode"] == "git":
        bot_source = f"{cfg['source_repo']} @ {cfg['source_ref']}"
    lines = [
        "Blank VPN bot install summary",
        "=============================",
        "",
        f"Bot source: {bot_source}",
        f"Bot dir: {cfg['bot_dir']}",
        f"RemnaWave dir: {cfg['remnawave_dir']}",
        f"Caddy dir: {cfg['caddy_dir']}",
        f"Cabinet dir: {cfg['cabinet_dir']}",
        "",
        f"Panel: https://{cfg['panel_domain']}",
        f"Subscription page: https://{cfg['sub_domain']}/connect",
        f"Cabinet: https://{cfg['cabinet_domain']}",
        f"API/webhooks: https://{cfg['api_domain']}",
        "",
        f"Telegram bot username: @{cfg['bot_username']}" if cfg["bot_username"] else "Telegram bot username: auto/unknown",
        f"Support: {cfg['support_username']} ({cfg['support_mode']})",
        "Default payment: Telegram Stars",
        "",
        f"RemnaWave admin: {cfg['remnawave_admin_username']} / {cfg['remnawave_admin_password']}",
        f"RemnaWave API token: {cfg['remnawave_api_key'] or 'not created because docker startup was skipped'}",
        f"Bot Web API token: {cfg['web_api_token']}",
        f"RemnaWave metrics: {cfg['metrics_user']} / {cfg['metrics_pass']}",
        "",
        "Installed commands:",
        "  add_payment",
        "  add_banner, list_banners, reset_banner",
        "  add_direct, add_cascade, add_inbound, add_routes, delete_node, change_sni",
        "",
    ]
    lines.extend(
        [
            "DNS note:",
            "  The subscription domain must remain DNS-only if Cloudflare is used.",
            "",
        ]
    )
    content = "\n".join(lines)
    print("\n" + content)
    if not ctx.dry_run:
        write_file(SUMMARY_PATH, content + "\n", mode=0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install a blank VPN bot control-plane")
    parser.add_argument("--answers", type=Path, help="JSON answers file for non-interactive installs")
    parser.add_argument("--reuse-answers", dest="reuse_answers", action="store_true", help="reuse saved answers.last.json")
    parser.add_argument("--no-reuse-answers", dest="reuse_answers", action="store_false", help="ask questions again")
    parser.set_defaults(reuse_answers=None)
    parser.add_argument("--source-mode", choices=["bundled", "git"], help="bot source mode; default is bundled")
    parser.add_argument("--source-repo", default="", help="external bot source Git URL when --source-mode=git")
    parser.add_argument("--source-ref", default=DEFAULT_SOURCE_REF, help="external bot source ref when --source-mode=git")
    parser.add_argument("--dry-run", action="store_true", help="print actions without changing the system")
    parser.add_argument("--skip-packages", action="store_true", help="packages are handled by shell bootstrap")
    parser.add_argument("--skip-docker-up", action="store_true", help="write configs but do not start containers")
    parser.add_argument("--skip-validation", action="store_true", help="skip docker compose and Caddy config validation")
    return parser


def apply_previous_answers_policy(ctx: InstallerContext, reuse_answers: bool | None) -> None:
    previous = load_json(ANSWERS_LAST_PATH)
    ctx.previous_answers = previous
    if ctx.answers_file or not previous:
        return
    should_reuse = reuse_answers
    if should_reuse is None:
        should_reuse = True
        if sys.stdin.isatty():
            should_reuse = yes_no(ctx, "reuse_previous_answers", f"Reuse previous install answers from {ANSWERS_LAST_PATH}", True)
    if should_reuse:
        ctx.answers = previous.copy()
        status(f"using previous install answers: {ANSWERS_LAST_PATH}")
    else:
        status("previous install answers will only be used to preserve generated secrets")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = InstallerContext(
        dry_run=args.dry_run,
        skip_packages=args.skip_packages,
        skip_docker_up=args.skip_docker_up,
        skip_validation=args.skip_validation,
        answers_file=args.answers,
        answers=load_json(args.answers) if args.answers else {},
        state=load_json(STATE_PATH),
        source_mode=args.source_mode,
        source_repo=args.source_repo,
        source_ref=args.source_ref,
    )
    apply_previous_answers_policy(ctx, args.reuse_answers)

    preflight(ctx)
    cfg = collect_answers(ctx)
    save_json(ANSWERS_LAST_PATH, cfg) if not ctx.dry_run else None
    setup_dns(ctx, cfg)
    prepare_bot_source(ctx, cfg)
    write_configs(ctx, cfg)
    install_default_banner_pack(ctx, cfg)
    prepare_bot_runtime_dirs(ctx, cfg)
    patch_caddy_compose(ctx, cfg)
    validate_generated_configs(ctx, cfg)
    open_firewall(ctx)
    docker_up_remnawave(ctx, cfg)
    bootstrap_remnawave_api_token(ctx, cfg)
    docker_up_application(ctx, cfg)
    install_aliases(ctx, cfg)
    install_installer_commands(ctx)
    bot_healthy = wait_for_health(ctx)
    write_summary(ctx, cfg)
    if not bot_healthy:
        die("Bot did not become healthy. Review the diagnostics above; installation is incomplete.")
    status("install flow completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
