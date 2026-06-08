from __future__ import annotations

import base64
import json
import importlib.util
import subprocess
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


installer = _load_installer()
payment_admin = _load_payment_admin()


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
    'news_channel_username': '@news',
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

    assert 'REMNAWAVE_API_URL=http://remnawave:3000' in env
    assert 'REMNAWAVE_API_URL=https://panel.example.com' not in env
    assert 'SUPPORT_USERNAME=@support' in env
    assert 'SUPPORT_SYSTEM_MODE=both' in env
    assert 'NEWS_CHANNEL_USERNAME=@news' in env
    assert 'DEFAULT_LANGUAGE=ru' in env
    assert 'AVAILABLE_LANGUAGES=ru,en' in env
    assert 'LANGUAGE_SELECTION_ENABLED=false' in env
    assert 'SKIP_RULES_ACCEPT=true' in env
    assert 'ENABLE_LOGO_MODE=false' in env
    assert 'TELEGRAM_STARS_ENABLED=true' in env
    assert 'YOOKASSA_ENABLED=false' in env
    assert 'DEFAULT_TARIFF_BOOTSTRAP_ENABLED=true' in env
    assert 'DEFAULT_TARIFF_BASIC_NAME=Базовый' in env
    assert 'DEFAULT_TARIFF_DARK_NAME=Темные списки' in env
    assert 'DEFAULT_TARIFF_TRIAL_NAME=Триал' in env


def test_empty_remnawave_token_writes_explicit_placeholder() -> None:
    env = installer.bot_env(CFG)
    sub_env = installer.remnawave_subscription_env(CFG)

    assert 'REMNAWAVE_API_KEY=FILL_REMNAWAVE_API_TOKEN_LATER' in env
    assert 'REMNAWAVE_API_TOKEN=FILL_REMNAWAVE_API_TOKEN_LATER' in sub_env


def test_caddyfile_contains_happ_headers() -> None:
    from blank_vpn_bot_installer.templates import caddyfile

    rendered = caddyfile('panel.example.com', 'sub.example.com', 'cabinet.example.com', 'api.example.com', 'admin@example.com')

    assert 'fragmentation-enable 1' in rendered
    assert 'no-limit-enabled 1' in rendered
    assert 'no-limit-xhttp-enabled 1' not in rendered
    assert 'header_up X-Forwarded-Proto https' in rendered
    assert 'header_up X-Forwarded-Ssl on' in rendered
    assert 'https://sub.example.com' in rendered


def test_bootstrap_script_keeps_prompts_interactive_when_piped() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts' / 'install_blank_vpn_bot.sh').read_text(encoding='utf-8')

    assert '[[ ! -t 0 && -r /dev/tty ]]' in script
    assert '< /dev/tty' in script
    assert 'blank_vpn_bot_installer/installer.py' in script


def test_bootstrap_script_waits_for_apt_locks() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts' / 'install_blank_vpn_bot.sh').read_text(encoding='utf-8')

    assert 'apt_get_retry()' in script
    assert "APT_LOCK_RETRIES:-60" in script
    assert "APT_LOCK_RETRY_DELAY_SECONDS:-10" in script
    assert "Could not get lock|Unable to acquire.*lock|is held by process" in script
    assert 'apt_get_retry update' in script
    assert 'apt_get_retry install -y --no-install-recommends' in script


def test_bundled_remnawave_compose_pins_stable_backend_major() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / 'bot-source' / 'ops' / 'remnawave' / 'docker-compose.yml').read_text(encoding='utf-8')

    assert 'image: remnawave/backend:2' in compose
    assert 'image: remnawave/backend:latest' not in compose


def test_remnawave_api_wait_restarts_unhealthy_backend_once(monkeypatch, tmp_path: Path) -> None:
    requests = 0
    restarts: list[list[str]] = []

    def fake_request(*_args, **_kwargs):
        nonlocal requests
        requests += 1
        if requests <= 18:
            raise OSError('API unavailable')
        return {'response': {'isRegisterAllowed': True}}

    def fake_health(container_name: str) -> str:
        return 'healthy' if container_name == 'remnawave-db' else 'starting'

    def fake_run(args, **_kwargs):
        restarts.append(args)
        return None

    monkeypatch.setattr(installer, 'remnawave_request', fake_request)
    monkeypatch.setattr(installer, 'docker_container_health', fake_health)
    monkeypatch.setattr(installer, 'run', fake_run)
    monkeypatch.setattr(installer.time, 'sleep', lambda _seconds: None)

    result = installer.wait_for_remnawave_api(installer.InstallerContext(), remnawave_dir=tmp_path)

    assert result == {'response': {'isRegisterAllowed': True}}
    assert restarts == [['docker', 'compose', 'restart', 'remnawave']]


def test_run_with_retries_recovers_after_transient_failure(monkeypatch, tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[int] = []

    def fake_run(args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer, 'run', fake_run)
    monkeypatch.setattr(installer.time, 'sleep', sleeps.append)

    result = installer.run_with_retries(
        ['docker', 'compose', 'pull'],
        cwd=tmp_path,
        attempts=4,
        delay_seconds=7,
    )

    assert result.returncode == 0
    assert attempts == 3
    assert sleeps == [7, 7]


def test_application_start_splits_pull_build_and_up(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path | None]] = []
    cfg = {
        'bot_dir': str(tmp_path / 'bot'),
        'remnawave_dir': str(tmp_path / 'remnawave'),
        'cabinet_dir': str(tmp_path / 'cabinet'),
        'caddy_dir': str(tmp_path / 'caddy'),
    }

    monkeypatch.setattr(
        installer,
        'run_with_retries',
        lambda args, *, cwd=None, **_kwargs: calls.append((args, cwd)),
    )
    monkeypatch.setattr(installer, 'docker_container_ready', lambda _name: False)
    monkeypatch.setattr(installer, 'mark', lambda *_args, **_kwargs: None)

    installer.docker_up_application(installer.InstallerContext(), cfg)

    assert (['docker', 'compose', 'pull', 'remnawave-subscription-page'], tmp_path / 'remnawave') in calls
    assert (['docker', 'compose', 'pull', 'postgres', 'redis'], tmp_path / 'bot') in calls
    assert (['docker', 'compose', 'build', 'bot'], tmp_path / 'bot') in calls
    assert (['docker', 'compose', 'up', '-d', '--pull', 'never'], tmp_path / 'bot') in calls
    assert (['docker', 'compose', 'build'], tmp_path / 'cabinet') in calls


def test_application_start_skips_already_running_stacks(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    cfg = {
        'bot_dir': str(tmp_path / 'bot'),
        'remnawave_dir': str(tmp_path / 'remnawave'),
        'cabinet_dir': str(tmp_path / 'cabinet'),
        'caddy_dir': str(tmp_path / 'caddy'),
    }

    monkeypatch.setattr(installer, 'docker_container_ready', lambda _name: True)
    monkeypatch.setattr(
        installer,
        'run_with_retries',
        lambda args, **_kwargs: calls.append(args),
    )
    monkeypatch.setattr(installer, 'mark', lambda *_args, **_kwargs: None)

    installer.docker_up_application(installer.InstallerContext(), cfg)

    assert calls == [['docker', 'compose', 'up', '-d', '--pull', 'never', 'bot']]


def test_bot_health_probe_supplies_api_key_from_container_env(monkeypatch) -> None:
    captured: list[str] = []

    def fake_run(args, **_kwargs):
        captured.extend(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(installer.subprocess, 'run', fake_run)

    result = installer.bot_health_probe()

    assert result is not None
    assert result.returncode == 0
    probe_code = captured[-1]
    assert "headers={'X-API-Key': os.environ['WEB_API_DEFAULT_TOKEN']}" in probe_code


def test_wait_for_health_accepts_healthy_docker_healthcheck(monkeypatch, capsys) -> None:
    monkeypatch.setattr(installer, 'docker_container_health', lambda _name: 'healthy')
    monkeypatch.setattr(installer, 'bot_health_probe', lambda: None)

    result = installer.wait_for_health(installer.InstallerContext())

    assert result is True
    assert 'bot health endpoint is ready' in capsys.readouterr().out


def test_preflight_rejects_existing_vpn_node(monkeypatch) -> None:
    original_exists = installer.Path.exists

    def fake_exists(path):
        if str(path) == '/opt/remnanode':
            return True
        return original_exists(path)

    monkeypatch.setattr(installer, 'ensure_root', lambda **_kwargs: None)
    monkeypatch.setattr(installer.shutil, 'which', lambda _cmd: '/usr/bin/tool')
    monkeypatch.setattr(installer, 'run', lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(installer.Path, 'exists', fake_exists)

    try:
        installer.preflight(installer.InstallerContext())
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError('existing VPN node must stop control-plane installation')


def test_docs_use_temp_file_for_interactive_bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = [
        root / 'README.md',
        root / 'docs' / 'operator-manual.md',
    ]

    for path in docs:
        rendered = path.read_text(encoding='utf-8')
        assert '| sudo bash' not in rendered
        assert '-o /tmp/install_blank_vpn_bot.sh' in rendered
        assert 'bash /tmp/install_blank_vpn_bot.sh' in rendered


def test_happ_routing_header_is_generated_from_structured_payload() -> None:
    from blank_vpn_bot_installer.templates import HAPP_ROUTING_HEADER, happ_routing_payload

    encoded = HAPP_ROUTING_HEADER.removeprefix('happ://routing/onadd/')
    payload = json.loads(base64.b64decode(encoded).decode('utf-8'))

    assert payload == happ_routing_payload()
    assert 'geosite:category-ru' in payload['DirectSites']
    assert 'domain:whoosh.bike' in payload['DirectSites']
    assert 'geoip:ru' in payload['DirectIp']
    assert payload['FakeDNS'] == 'false'


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


def test_prompt_domain_reprompts_after_empty_value(monkeypatch, capsys) -> None:
    answers = iter(['', 'example.com'])
    monkeypatch.setattr('builtins.input', lambda _prompt: next(answers))

    value = installer.prompt_domain(installer.InstallerContext(), 'root_domain', 'Root domain')

    assert value == 'example.com'
    assert 'Domain is required' in capsys.readouterr().out


def test_prompt_domain_rejects_invalid_answers_file_value() -> None:
    ctx = installer.InstallerContext(answers={'root_domain': ''})

    try:
        installer.prompt_domain(ctx, 'root_domain', 'Root domain')
    except ValueError as exc:
        assert str(exc) == 'Domain is required'
    else:
        raise AssertionError('invalid non-interactive domain should fail')


def test_yes_no_accepts_latin_and_cyrillic_yes(monkeypatch) -> None:
    for answer in ('y', 'у'):
        monkeypatch.setattr('builtins.input', lambda _prompt, answer=answer: answer)
        assert installer.yes_no(installer.InstallerContext(), 'confirm', 'Confirm', False) is True


def test_remnawave_request_supplies_required_https_proxy_headers(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"response": {"isRegisterAllowed": true}}'

    def fake_urlopen(request, timeout):
        captured['headers'] = {key.lower(): value for key, value in request.header_items()}
        captured['timeout'] = timeout
        return Response()

    monkeypatch.setattr(installer.urllib.request, 'urlopen', fake_urlopen)

    result = installer.remnawave_request('GET', '/api/auth/status', timeout=5)

    assert result == {'response': {'isRegisterAllowed': True}}
    assert captured['headers']['x-forwarded-for'] == '127.0.0.1'
    assert captured['headers']['x-forwarded-proto'] == 'https'
    assert captured['headers']['x-remnawave-client-type'] == 'browser'
    assert captured['timeout'] == 5


def test_response_value_extracts_remnawave_token() -> None:
    response = {'response': {'token': 'rw-token', 'accessToken': 'jwt-token'}}

    assert installer.response_value(response, 'response', 'token') == 'rw-token'
    assert installer.response_value(response, 'response', 'accessToken') == 'jwt-token'
    assert installer.response_value(response, 'missing', 'token') is None


def test_bundled_bot_has_clean_onboarding_and_navigation() -> None:
    root = Path(__file__).resolve().parents[1]
    installer_source = (root / 'blank_vpn_bot_installer' / 'installer.py').read_text(encoding='utf-8')
    keyboard_source = (root / 'bot-source' / 'app' / 'keyboards' / 'inline.py').read_text(encoding='utf-8')
    info_source = keyboard_source.split('def get_info_menu_keyboard(', 1)[1].split(
        'def get_happ_download_button_row(', 1
    )[0]
    resources_source = keyboard_source.split('def get_resources_keyboard(', 1)[1]

    assert 'install_default_banner_pack' not in installer_source
    assert 'RESOURCES_GUIDES' not in resources_source
    assert 'RESOURCES_WEBSITE' not in resources_source
    assert 'https://t.me/example' not in resources_source
    assert 'MENU_PRIVACY_POLICY' not in info_source
    assert 'MENU_PUBLIC_OFFER' not in info_source
    assert 'USER_AGREEMENT' not in info_source


def test_legacy_default_banner_pack_is_removed_once(tmp_path: Path, monkeypatch) -> None:
    from blank_vpn_bot_installer.banner_pack import banner_targets

    cfg = {'bot_dir': str(tmp_path / 'bot')}
    ctx = installer.InstallerContext(state={'default_banners_installed': {'count': 22}})
    monkeypatch.setattr(installer, 'STATE_PATH', tmp_path / 'state.json')
    banners_dir = Path(cfg['bot_dir']) / 'app' / 'assets' / 'banners'
    cache_path = Path(cfg['bot_dir']) / 'data' / 'banner_file_ids.json'
    banners_dir.mkdir(parents=True)
    cache_path.parent.mkdir(parents=True)
    banner_path = banners_dir / banner_targets()[0][2]
    banner_path.write_bytes(b'old-default')
    cache_path.write_text('{}', encoding='utf-8')

    installer.remove_legacy_default_banner_pack(ctx, cfg)

    assert not banner_path.exists()
    assert not cache_path.exists()
    assert ctx.state['default_banners_installed'] is False


def test_prepare_bot_runtime_dirs_assigns_container_user(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    cfg = {'bot_dir': str(tmp_path / 'bot')}
    monkeypatch.setattr(installer, 'run', lambda args, **_kwargs: calls.append(args))

    installer.prepare_bot_runtime_dirs(installer.InstallerContext(), cfg)

    runtime_dirs = [tmp_path / 'bot' / name for name in ('data', 'logs', 'uploads', 'locales')]
    assert all(path.is_dir() for path in runtime_dirs)
    assert calls == [['chown', '-R', '1000:1000', *(str(path) for path in runtime_dirs)]]


def test_install_summary_labels_subscription_base_as_user_specific(monkeypatch, tmp_path: Path) -> None:
    cfg = dict(CFG)
    summary_path = tmp_path / 'summary.txt'
    monkeypatch.setattr(installer, 'SUMMARY_PATH', summary_path)

    installer.write_summary(installer.InstallerContext(), cfg)

    rendered = summary_path.read_text(encoding='utf-8')
    assert 'Subscription URL base: https://sub.example.com/connect/<user-short-uuid>' in rendered
    assert 'Subscription page: https://sub.example.com/connect' not in rendered


def test_bundled_bot_source_supports_discord_warp_toggle() -> None:
    root = Path(__file__).resolve().parents[1]
    schemas = root / 'bot-source' / 'app' / 'templar_node' / 'schemas.py'
    profile = root / 'bot-source' / 'app' / 'templar_node' / 'xray_profile.py'

    assert 'discord_direct: bool = True' in schemas.read_text(encoding='utf-8')
    assert 'if config.warp.discord_direct:' in profile.read_text(encoding='utf-8')


def test_bundled_bot_source_uses_tcp_reality_default_with_server_keepalive() -> None:
    root = Path(__file__).resolve().parents[1]
    schemas = (root / 'bot-source' / 'app' / 'templar_node' / 'schemas.py').read_text(encoding='utf-8')
    builder = (root / 'bot-source' / 'app' / 'templar_node' / 'config_builder.py').read_text(encoding='utf-8')
    profile = (root / 'bot-source' / 'app' / 'templar_node' / 'xray_profile.py').read_text(encoding='utf-8')
    render = (root / 'bot-source' / 'app' / 'templar_node' / 'render.py').read_text(encoding='utf-8')
    remnawave = (root / 'bot-source' / 'app' / 'templar_node' / 'remnawave.py').read_text(encoding='utf-8')

    assert "DEFAULT_XHTTP_SERVER_EXTRA: dict[str, Any]" in schemas
    assert "'scStreamUpServerSecs': '20-40'" in schemas
    assert "transport: RealityTransport = RealityTransport.TCP" in schemas
    assert "return {'transport': 'tcp'}" in builder
    assert "transport': 'xhttp'" not in builder
    assert "STREAM_KEEPALIVE_SOCKOPT" in profile
    assert "'sockopt': _stream_keepalive_sockopt()" in profile
    assert "TELEGRAM_WARP_IPS = ('geoip:telegram',)" in profile
    assert "if config.country_code == 'RU':" in profile
    assert 'rules.extend(_telegram_warp_rules(inbound_tags=inbound_tags, outbound_tag=warp_tag))' in profile
    assert "'network': 'udp', 'outboundTag': DIRECT_TAG" in profile
    assert "'connIdle': 1800" in profile
    assert "'handshake': 10" in profile
    assert 'net.ipv4.tcp_keepalive_time = 60' in render
    assert 'net.ipv4.tcp_keepalive_intvl = 10' in render
    assert 'net.ipv4.tcp_keepalive_probes = 6' in render
    assert "body['path'] = ''" in remnawave
    assert "body['host'] = ''" in remnawave


def test_bundled_bot_source_bounds_ssh_bootstrap_connects() -> None:
    root = Path(__file__).resolve().parents[1]
    layer1 = (root / 'bot-source' / 'app' / 'templar_node' / 'layer1.py').read_text(encoding='utf-8')

    assert "'ConnectTimeout=20'" in layer1
    assert "'ConnectionAttempts=1'" in layer1
    assert "'ServerAliveInterval=15'" in layer1
    assert "'ServerAliveCountMax=2'" in layer1


def test_bundled_bot_source_keeps_ru_edge_transit_user_on_full_delete() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (root / 'bot-source' / 'app' / 'templar_node' / 'cli.py').read_text(encoding='utf-8')

    assert 'from app.templar_node.schemas import NodeRole' in cli
    assert 'config.role != NodeRole.RU_EDGE' in cli


def test_bundled_bot_source_bootstraps_default_tariffs() -> None:
    root = Path(__file__).resolve().parents[1]
    config = root / 'bot-source' / 'app' / 'config.py'
    tariff_crud = root / 'bot-source' / 'app' / 'database' / 'crud' / 'tariff.py'
    config_text = config.read_text(encoding='utf-8')
    tariff_text = tariff_crud.read_text(encoding='utf-8')

    assert "DEFAULT_TARIFF_BOOTSTRAP_ENABLED: bool = True" in config_text
    assert "DEFAULT_TARIFF_BASIC_NAME: str = 'Базовый'" in config_text
    assert "DEFAULT_TARIFF_DARK_NAME: str = 'Темные списки'" in config_text
    assert "DEFAULT_TARIFF_TRIAL_NAME: str = 'Триал'" in config_text
    assert 'async def ensure_blank_default_tariffs' in tariff_text
    assert "trial_name = settings.DEFAULT_TARIFF_TRIAL_NAME.strip() or 'Триал'" in tariff_text


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


def test_bundled_node_quick_defaults_are_neutral() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (root / 'bot-source' / 'app' / 'templar_node' / 'cli.py').read_text(encoding='utf-8')

    assert "DEFAULT_QUICK_MAIN_IPV4 = '203.0.113.10'" in cli
    assert "DEFAULT_QUICK_REMNAWAVE_API_URL = 'https://panel.example.com'" in cli
    assert '213.155.11.125' not in cli
    assert 'templarvpn.com' not in cli


def test_bundled_user_visible_branding_is_neutralized() -> None:
    root = Path(__file__).resolve().parents[1]
    user_visible_files = [
        root / 'bot-source' / 'Dockerfile',
        root / 'bot-source' / 'main.py',
        root / 'bot-source' / 'app' / 'middlewares' / 'global_error.py',
        root / 'bot-source' / 'app' / 'services' / 'startup_notification_service.py',
        root / 'bot-source' / 'app' / 'handlers' / 'support.py',
        root / 'bot-source' / 'app' / 'localization' / 'default_locales' / 'ru.yml',
        root / 'bot-source' / 'app' / 'localization' / 'default_locales' / 'en.yml',
        root / 'bot-source' / 'app' / 'localization' / 'locales' / 'ru.json',
        root / 'bot-source' / 'app' / 'localization' / 'locales' / 'en.json',
    ]
    forbidden = [
        'Remnawave Bedolaga Bot',
        'Bedolaga RemnaWave Bot',
        'Templar VPN Support',
        'Поддержка Templar VPN',
        'Сообщить разработчику',
        'Поставить звезду',
    ]

    for path in user_visible_files:
        rendered = path.read_text(encoding='utf-8')
        for phrase in forbidden:
            assert phrase not in rendered


def test_node_shortcut_prefers_dedicated_node_venv() -> None:
    root = Path(__file__).resolve().parents[1]
    alias = root / 'bot-source' / 'scripts' / 'templar_node_alias'
    rendered = alias.read_text(encoding='utf-8')

    assert 'PYTHON_BIN="${TEMPLAR_NODE_PYTHON:-$REPO_DIR/.venv-templar-node/bin/python}"' in rendered
    assert 'if [ -x "$REPO_DIR/.venv/bin/python" ]; then' in rendered


def test_preserved_or_generated_reuses_previous_secret() -> None:
    ctx = installer.InstallerContext(previous_answers={'web_api_token': 'old-token'})

    assert installer.preserved_or_generated(ctx, 'web_api_token', lambda: 'new-token') == 'old-token'


def test_collect_answers_defaults_to_bundled_source_without_prompt() -> None:
    answers = {
        'install_root': '/opt',
        'project_name': 'VPN Service',
        'server_ip': '203.0.113.10',
        'root_domain': 'example.com',
        'panel_domain': 'panel.example.com',
        'sub_domain': 'sub.example.com',
        'cabinet_domain': 'cabinet.example.com',
        'api_domain': 'api.example.com',
        'le_email': 'admin@example.com',
        'dns_mode': 'manual',
        'bot_token': '123456:token',
        'bot_username': 'vpn_bot',
        'admin_ids': '123',
        'support_username': '@support',
        'news_channel_username': '',
        'support_mode': 'both',
        'telegram_stars_rate_rub': '1.3',
        'remnawave_admin_username': 'admin',
        'remnawave_admin_password': 'GeneratedPassword1234567890',
        'remnawave_api_key': '',
    }
    ctx = installer.InstallerContext(dry_run=True, answers=answers)

    cfg = installer.collect_answers(ctx)

    assert cfg['source_mode'] == 'bundled'
    assert cfg['source_repo'] == ''
    assert cfg['source_ref'] == 'main'


def test_apply_previous_answers_policy_reuses_saved_answers(tmp_path: Path, monkeypatch) -> None:
    answers_path = tmp_path / 'answers.last.json'
    installer.save_json(answers_path, {'bot_token': 'old-token', 'web_api_token': 'old-web-token'})
    monkeypatch.setattr(installer, 'ANSWERS_LAST_PATH', answers_path)
    ctx = installer.InstallerContext()

    installer.apply_previous_answers_policy(ctx, True)

    assert ctx.answers['bot_token'] == 'old-token'
    assert ctx.previous_answers['web_api_token'] == 'old-web-token'
