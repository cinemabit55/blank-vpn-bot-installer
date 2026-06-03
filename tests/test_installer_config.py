from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_installer():
    path = Path(__file__).resolve().parents[1] / 'blank_vpn_bot_installer' / 'installer.py'
    spec = importlib.util.spec_from_file_location('installer', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


CFG = {
    'source_repo': 'git@github.com:cinemabit55/templarvpn.git',
    'source_ref': 'main',
    'bot_dir': '/opt/bedolaga',
    'remnawave_dir': '/opt/remnawave',
    'caddy_dir': '/opt/caddy-remnawave',
    'cabinet_dir': '/opt/cabinet',
    'project_name': 'VPN Service',
    'server_ip': '203.0.113.10',
    'root_domain': 'example.com',
    'panel_domain': 'panel.example.com',
    'sub_domain': 'sub.example.com',
    'cabinet_domain': 'cabinet.example.com',
    'api_domain': 'api.example.com',
    'le_email': 'admin@example.com',
    'dns_mode': 'manual',
    'cloudflare_token': '',
    'cloudflare_proxy_public': False,
    'bot_token': '123456:token',
    'bot_username': 'vpn_bot',
    'admin_ids': '123',
    'support_username': '@support',
    'support_mode': 'both',
    'telegram_stars_rate_rub': '1.3',
    'remnawave_api_key': '',
    'postgres_password': 'bot-db-password',
    'remnawave_postgres_password': 'panel-db-password',
    'web_api_token': 'web-api-token',
    'web_api_hmac': 'web-api-hmac',
    'cabinet_jwt_secret': 'cabinet-secret',
    'remnawave_jwt_secret': 'panel-jwt',
    'remnawave_api_tokens_secret': 'panel-api-secret',
    'remnawave_webhook_secret': 'panel-webhook-secret',
    'metrics_user': 'metrics',
    'metrics_pass': 'metrics-pass',
}


def test_support_mode_written_to_bot_env() -> None:
    env = installer.bot_env(CFG)

    assert 'SUPPORT_USERNAME=@support' in env
    assert 'SUPPORT_SYSTEM_MODE=both' in env
    assert 'TELEGRAM_STARS_ENABLED=true' in env
    assert 'YOOKASSA_ENABLED=false' in env


def test_empty_remnawave_token_writes_explicit_placeholder() -> None:
    env = installer.bot_env(CFG)
    sub_env = installer.remnawave_subscription_env(CFG)

    assert 'REMNAWAVE_API_KEY=FILL_REMNAWAVE_API_TOKEN_LATER' in env
    assert 'REMNAWAVE_API_TOKEN=FILL_REMNAWAVE_API_TOKEN_LATER' in sub_env


def test_caddyfile_contains_happ_headers() -> None:
    from blank_vpn_bot_installer.templates import caddyfile

    rendered = caddyfile('panel.example.com', 'sub.example.com', 'cabinet.example.com', 'api.example.com', 'admin@example.com')

    assert 'fragmentation-enable 1' in rendered
    assert 'no-limit-xhttp-enabled 1' in rendered
    assert 'https://sub.example.com' in rendered
