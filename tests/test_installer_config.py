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


def _load_payment_admin():
    path = Path(__file__).resolve().parents[1] / 'blank_vpn_bot_installer' / 'payment_admin.py'
    spec = importlib.util.spec_from_file_location('payment_admin', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_backup_admin():
    path = Path(__file__).resolve().parents[1] / 'blank_vpn_bot_installer' / 'backup_admin.py'
    spec = importlib.util.spec_from_file_location('backup_admin', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()
payment_admin = _load_payment_admin()
backup_admin = _load_backup_admin()
from blank_vpn_bot_installer.banner_pack import banner_targets, install_default_banners


CFG = {
    'source_mode': 'bundled',
    'source_repo': '',
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
    'remnawave_admin_username': 'admin',
    'remnawave_admin_password': 'GeneratedPassword1234567890',
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


def test_payment_provider_defaults_include_yookassa_urls() -> None:
    env = {
        'WEBHOOK_URL': 'https://api.example.com',
        'CABINET_URL': 'https://cabinet.example.com',
    }
    providers = payment_admin.build_providers(env)
    yookassa = providers['yookassa']

    assert yookassa.enabled_key == 'YOOKASSA_ENABLED'
    assert yookassa.sub_options == {'card': True, 'sbp': True}
    assert any(field.key == 'YOOKASSA_RETURN_URL' and field.default == 'https://cabinet.example.com' for field in yookassa.fields)


def test_update_env_file_preserves_existing_lines(tmp_path: Path) -> None:
    env_path = tmp_path / '.env'
    env_path.write_text('BOT_TOKEN=token\nYOOKASSA_ENABLED=false\n# keep me\n', encoding='utf-8')

    backup = payment_admin.update_env_file(
        env_path,
        {
            'YOOKASSA_ENABLED': 'true',
            'YOOKASSA_SHOP_ID': 'shop',
        },
    )

    rendered = env_path.read_text(encoding='utf-8')
    assert backup.exists()
    assert 'YOOKASSA_ENABLED=true' in rendered
    assert 'YOOKASSA_SHOP_ID=shop' in rendered
    assert '# keep me' in rendered


def test_payment_config_sql_enables_method() -> None:
    providers = payment_admin.build_providers({})
    sql = payment_admin.payment_config_sql(providers['cryptobot'], 'CryptoBot')

    assert "method_id = 'cryptobot'" in sql
    assert 'is_enabled = TRUE' in sql
    assert "display_name = 'CryptoBot'" in sql


def test_random_remnawave_password_matches_panel_rules() -> None:
    password = installer.random_remnawave_admin_password()

    assert len(password) >= 24
    assert any(char.isupper() for char in password)
    assert any(char.islower() for char in password)
    assert any(char.isdigit() for char in password)


def test_response_value_extracts_remnawave_token() -> None:
    response = {'response': {'token': 'rw-token', 'accessToken': 'jwt-token'}}

    assert installer.response_value(response, 'response', 'token') == 'rw-token'
    assert installer.response_value(response, 'response', 'accessToken') == 'jwt-token'
    assert installer.response_value(response, 'missing', 'token') is None


def test_default_banner_pack_targets_all_expected_files(tmp_path: Path) -> None:
    targets = banner_targets()
    filenames = {filename for _slot, _language, filename in targets}

    assert len(targets) == 22
    assert 'main_menu_ru.jpg' in filenames
    assert 'profile_en.jpg' in filenames
    assert 'welcome.jpg' in filenames
    assert install_default_banners(tmp_path, 'VPN Service', dry_run=True) == 22


def test_bundled_bot_source_supports_discord_warp_toggle() -> None:
    root = Path(__file__).resolve().parents[1]
    schemas = root / 'bot-source' / 'app' / 'templar_node' / 'schemas.py'
    profile = root / 'bot-source' / 'app' / 'templar_node' / 'xray_profile.py'

    assert 'discord_direct: bool = True' in schemas.read_text(encoding='utf-8')
    assert 'if config.warp.discord_direct:' in profile.read_text(encoding='utf-8')


def test_copy_tree_preserves_existing_runtime_files(tmp_path: Path) -> None:
    src = tmp_path / 'src'
    dst = tmp_path / 'dst'
    src.mkdir()
    dst.mkdir()
    (src / 'app.py').write_text('new code', encoding='utf-8')
    (dst / 'data').mkdir()
    (dst / 'data' / 'runtime.json').write_text('{}', encoding='utf-8')

    installer.copy_tree(src, dst)

    assert (dst / 'app.py').read_text(encoding='utf-8') == 'new code'
    assert (dst / 'data' / 'runtime.json').read_text(encoding='utf-8') == '{}'


def test_bundled_shortcut_help_is_unbranded() -> None:
    root = Path(__file__).resolve().parents[1]
    user_visible_files = [
        root / 'bot-source' / 'scripts' / 'templar_bot_alias',
        root / 'bot-source' / 'scripts' / 'templar_node_alias',
        root / 'bot-source' / 'scripts' / 'banner_admin.py',
        root / 'bot-source' / 'scripts' / 'install_templar_bot_aliases.sh',
        root / 'bot-source' / 'scripts' / 'install_templar_node_aliases.sh',
    ]

    for path in user_visible_files:
        assert 'Templar' not in path.read_text(encoding='utf-8')


def test_backup_archive_includes_static_install_files(tmp_path: Path) -> None:
    bot_dir = tmp_path / 'bedolaga'
    remnawave_dir = tmp_path / 'remnawave'
    caddy_dir = tmp_path / 'caddy-remnawave'
    cabinet_dir = tmp_path / 'cabinet'
    state_dir = tmp_path / 'state'
    for path in (bot_dir, remnawave_dir, caddy_dir, cabinet_dir, state_dir):
        path.mkdir()
    (bot_dir / '.env').write_text('BOT_TOKEN=token\n', encoding='utf-8')
    (remnawave_dir / '.env').write_text('POSTGRES_DB=postgres\n', encoding='utf-8')
    (caddy_dir / 'Caddyfile').write_text('example.com {}\n', encoding='utf-8')
    (cabinet_dir / '.env').write_text('VITE_APP_NAME=VPN\n', encoding='utf-8')
    (state_dir / 'install-summary.txt').write_text('summary\n', encoding='utf-8')

    archive = backup_admin.create_backup(
        backup_admin.BackupPaths(
            bot_dir=bot_dir,
            remnawave_dir=remnawave_dir,
            caddy_dir=caddy_dir,
            cabinet_dir=cabinet_dir,
            state_dir=state_dir,
            output_dir=tmp_path / 'backups',
        ),
        skip_db=True,
    )

    assert archive.exists()
    assert archive.stat().st_mode & 0o777 == 0o600
