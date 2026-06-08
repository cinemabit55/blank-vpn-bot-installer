"""Command line interface for Templar node onboarding."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

import yaml

from app.templar_node.availability import (
    SshRemoteCommandRunner,
    run_main_availability_check,
    run_ru_edge_foreign_exit_check,
)
from app.templar_node.bedolaga import DatabaseBedolagaAdapter, LocalBedolagaAdapter, PsqlBedolagaAdapter
from app.templar_node.cloudflare import (
    CloudflareCheckError,
    CloudflareUpsertError,
    run_cloudflare_check,
    upsert_node_dns_records,
)
from app.templar_node.config_builder import (
    DEFAULT_REMOTE_DEST_SERVER_NAME,
    DEFAULT_REMOTE_DEST_TARGET,
    CascadeDirectInput,
    CommonGenerationInput,
    ConfigBuildError,
    ExtraRuEdgeInput,
    NodeGenerationInput,
    RuWarpInput,
    TariffTargets,
    build_foreign_config_with_extra_ru_edge,
    generate_cascade_direct,
    generate_extra_ru_edge,
    generate_ru_warp,
    write_generated_configs,
    write_yaml_config,
)
from app.templar_node.decommission import DecommissionError, run_decommission
from app.templar_node.layer1 import (
    Layer1Error,
    Layer1SshBootstrapOptions,
    run_layer1_local_bootstrap,
    run_layer1_ssh_bootstrap,
)
from app.templar_node.layer2a import Layer2aError, run_layer2a_pre_bootstrap
from app.templar_node.layer2b import Layer2bError, run_layer2b_post_bootstrap
from app.templar_node.loader import NodeConfigLoadError, load_node_config
from app.templar_node.operations import (
    OperatorContext,
    OperatorError,
    _public_dns_ipv6,
    apply_routing_overrides,
    run_cascade_direct_operator,
    run_extra_ru_edge_operator,
    run_routing_add_operator,
    run_ru_direct_operator,
)
from app.templar_node.planner import build_plan
from app.templar_node.remnawave import DiscoveredRemnaWaveAdapter, HttpRemnaWaveAdapter, LocalRemnaWaveAdapter, RemnaWaveAdapterError
from app.templar_node.remnawave_probe import (
    RemnaWaveProbeAuth,
    RemnaWaveProbeError,
    fetch_remnawave_node_secret_key,
    run_remnawave_probe,
)
from app.templar_node.render import render_bundle, write_bundle
from app.templar_node.rotation import (
    DomainRotationError,
    build_domain_rotation_plan,
    build_sni_change_plan,
    mark_domain_rotation_switch_failed,
    mark_domain_rotation_switch_started,
    mark_domain_rotation_switch_succeeded,
    rotate_reality_secret_server_names,
    save_domain_rotation_state,
    write_rotated_config,
    write_sni_changed_config,
)
from app.templar_node.routes import RouteOverrideError, RouteOverrideStore
from app.templar_node.schemas import NodeRole
from app.templar_node.secrets import LocalSecretStore, SecretStoreError
from app.templar_node.simulation import FakeEnvironmentStore, SimulationError, simulate_onboarding
from app.templar_node.state import LAYER2B_CHECKPOINTS, NodeStateStore, StateStoreError
from app.templar_node.synthetic import DEFAULT_SYNTHETIC_PROBE_URL, run_synthetic_vpn_check
from app.templar_node.warp import (
    WarpRegistrationError,
    WarpRegistrationOptions,
    ensure_warp_registration_for_config,
)


DEFAULT_STATE_DIR = Path('/var/lib/templar-onboarding/nodes')
DEFAULT_DECOMMISSION_CONFIG_DIRS = (
    Path('/var/lib/templar-node-test/configs'),
    Path('/var/lib/templar-onboarding/configs'),
    Path('/opt/templar/configs'),
)
DEFAULT_NODE_SECRETS_DIR = Path('/opt/templar/secrets')
DEFAULT_REMNAWAVE_API_KEY_REF = 'secrets/remnawave-api-key'
DEFAULT_REMNAWAVE_CADDY_TOKEN_REF = 'secrets/remnawave-caddy-token'
DEFAULT_ADMIN_PRIVATE_KEY_REF = 'secrets/ssh-admin-private-key'
DEFAULT_QUICK_WORK_ROOT = Path('/var/lib/templar-onboarding')
DEFAULT_QUICK_MAIN_IPV4 = '203.0.113.10'
DEFAULT_QUICK_REMNAWAVE_API_URL = 'https://panel.example.com'
DEFAULT_QUICK_ADMIN_ALLOWLIST = (DEFAULT_QUICK_MAIN_IPV4,)
DEFAULT_QUICK_TARIFF_PRESETS = (
    ('1', 'Базовый', ('Базовый',), False),
    ('2', 'Темные списки', ('Темные списки',), False),
    ('3', 'Триал', (), True),
    ('4', 'Базовый + Темные списки', ('Базовый', 'Темные списки'), False),
    ('5', 'Базовый + Триал', ('Базовый',), True),
    ('6', 'Темные списки + Триал', ('Темные списки',), True),
    ('7', 'Все три', ('Базовый', 'Темные списки'), True),
)


def _status_line(message: str) -> None:
    print(f'[status] {message}', flush=True)



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NodeConfigLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='templar-node',
        description='VPN node onboarding helper.',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    validate_parser = subparsers.add_parser('validate', help='Validate a node YAML config.')
    _add_config_arg(validate_parser)
    validate_parser.set_defaults(func=_cmd_validate)

    plan_parser = subparsers.add_parser('plan', help='Print a dry-run onboarding plan.')
    _add_config_arg(plan_parser)
    plan_parser.add_argument('--format', choices=('text', 'json'), default='text')
    plan_parser.set_defaults(func=_cmd_plan)

    render_parser = subparsers.add_parser('render', help='Render local bootstrap artifacts.')
    _add_config_arg(render_parser)
    render_parser.add_argument('--output-dir', type=Path, required=True, help='Directory to write rendered artifacts into.')
    render_parser.add_argument('--secrets-dir', type=Path, help='Optional local root for resolving real secret values.')
    render_parser.set_defaults(func=_cmd_render)

    generate_parser = subparsers.add_parser('generate', help='Generate node YAML configs from CLI inputs.')
    generate_subparsers = generate_parser.add_subparsers(dest='scenario', required=True)
    cascade_parser = generate_subparsers.add_parser(
        'cascade-direct',
        help='Generate FOREIGN-EXIT + RU-EDGE configs for cascade and foreign direct.',
    )
    _add_generation_common_args(cascade_parser)
    _add_tariff_args(cascade_parser, prefix='')
    _add_tariff_args(cascade_parser, prefix='foreign')
    _add_tariff_args(cascade_parser, prefix='ru')
    _add_node_generation_args(cascade_parser, prefix='foreign', country_default=None, domain_required=False)
    _add_node_generation_args(cascade_parser, prefix='ru', country_default='RU', domain_required=False)
    _add_foreign_reality_args(cascade_parser)
    _add_ru_edge_reality_args(cascade_parser)
    cascade_parser.add_argument('--service-user', default='bridge_ru_to_foreign')
    cascade_parser.set_defaults(func=_cmd_generate_cascade_direct)

    extra_ru_edge_parser = generate_subparsers.add_parser(
        'ru-edge',
        help='Generate one more RU-EDGE config for an existing FOREIGN-EXIT cascade.',
    )
    extra_ru_edge_parser.add_argument('foreign_config', type=Path, help='Existing foreign-exit YAML config to reuse for transit.')
    extra_ru_edge_parser.add_argument('--output-dir', type=Path, required=True)
    extra_ru_edge_parser.add_argument(
        '--updated-foreign-config',
        type=Path,
        help='Optional path for a copy of foreign_config with this RU IPv4 appended to transit.allow_from.',
    )
    _add_tariff_args(extra_ru_edge_parser, prefix='')
    _add_node_generation_args(extra_ru_edge_parser, prefix='', country_default='RU', domain_required=False)
    _add_ru_warp_reality_args(extra_ru_edge_parser)
    extra_ru_edge_parser.set_defaults(func=_cmd_generate_extra_ru_edge)

    ru_warp_parser = generate_subparsers.add_parser('ru-warp', help='Generate a RU direct-through-WARP config.')
    _add_generation_common_args(ru_warp_parser)
    _add_tariff_args(ru_warp_parser, prefix='')
    _add_node_generation_args(ru_warp_parser, prefix='', country_default='RU', domain_required=False)
    _add_ru_warp_reality_args(ru_warp_parser)
    ru_warp_parser.set_defaults(func=_cmd_generate_ru_warp)

    wizard_parser = subparsers.add_parser('wizard', help='Interactive config generator.')
    wizard_subparsers = wizard_parser.add_subparsers(dest='scenario', required=True)
    wizard_cascade_parser = wizard_subparsers.add_parser('cascade-direct', help='Prompt for FOREIGN-EXIT + RU-EDGE configs.')
    wizard_cascade_parser.add_argument('--output-dir', type=Path, required=True)
    wizard_cascade_parser.set_defaults(func=_cmd_wizard_cascade_direct)
    wizard_ru_warp_parser = wizard_subparsers.add_parser('ru-warp', help='Prompt for a RU WARP config.')
    wizard_ru_warp_parser.add_argument('--output-dir', type=Path, required=True)
    wizard_ru_warp_parser.set_defaults(func=_cmd_wizard_ru_warp)

    quick_parser = subparsers.add_parser('quick', help='Interactive one-command provisioning flows.')
    quick_subparsers = quick_parser.add_subparsers(dest='quick_scenario', required=True)

    quick_cascade_parser = quick_subparsers.add_parser(
        'cascade-direct',
        help='Ask for two VPSes, generate cascade+direct configs, and run onboarding.',
    )
    _add_quick_common_args(quick_cascade_parser)
    quick_cascade_parser.set_defaults(func=_cmd_quick_cascade_direct)

    quick_direct_site_parser = quick_subparsers.add_parser(
        'direct-site',
        help='Ask for one RU VPS with WARP and a node domain/decoy site, then run onboarding.',
    )
    _add_quick_common_args(quick_direct_site_parser)
    quick_direct_site_parser.set_defaults(func=_cmd_quick_direct_site)

    quick_direct_remote_parser = quick_subparsers.add_parser(
        'direct-remote',
        help='Ask for one RU VPS with WARP and remote_dest/no local site, then run onboarding.',
    )
    _add_quick_common_args(quick_direct_remote_parser)
    quick_direct_remote_parser.set_defaults(func=_cmd_quick_direct_remote)

    quick_extra_ru_edge_parser = quick_subparsers.add_parser(
        'extra-ru-edge',
        help='Select a FOREIGN-EXIT, add another RU-edge without a node site, and run onboarding.',
    )
    quick_extra_ru_edge_parser.add_argument('foreign_config', type=Path, nargs='?', help='Existing foreign-exit YAML. Omit for selection menu.')
    _add_quick_common_args(quick_extra_ru_edge_parser)
    quick_extra_ru_edge_parser.set_defaults(func=_cmd_quick_extra_ru_edge)

    quick_routing_parser = quick_subparsers.add_parser(
        'routing-add',
        help='Select a RU-edge and add route overrides, applying them to RemnaWave by default.',
    )
    quick_routing_parser.add_argument('config', type=Path, nargs='?', help='RU-edge YAML. Omit for selection menu.')
    _add_quick_common_args(quick_routing_parser)
    quick_routing_parser.add_argument('--no-apply', action='store_true', help='Only write routes.yml; do not push profile changes.')
    quick_routing_parser.set_defaults(func=_cmd_quick_routing_add)

    quick_sni_parser = quick_subparsers.add_parser(
        'sni-change',
        help='Select a remote_dest node and change its REALITY target/SNI, applying RemnaWave by default.',
    )
    quick_sni_parser.add_argument('config', type=Path, nargs='?', help='remote_dest node YAML. Omit for selection menu.')
    _add_quick_common_args(quick_sni_parser)
    quick_sni_parser.add_argument('--target', help='New remote REALITY dest host:port, for example yahoo.com:443.')
    quick_sni_parser.add_argument('--server-name', action='append', default=[], help='REALITY serverName/SNI. Defaults to target host.')
    quick_sni_parser.add_argument('--no-apply', action='store_true', help='Only update YAML and REALITY secret; do not push RemnaWave.')
    quick_sni_parser.set_defaults(func=_cmd_quick_sni_change)

    simulate_parser = subparsers.add_parser('simulate', help='Run a local fake end-to-end onboarding.')
    simulate_parser.add_argument('configs', type=Path, nargs='+', help='One or more node config YAML files.')
    simulate_parser.add_argument('--env-dir', type=Path, required=True, help='Directory for fake RemnaWave/Bedolaga state.')
    simulate_parser.add_argument('--state-dir', type=Path, required=True, help='Directory for node checkpoint state.')
    simulate_parser.add_argument('--render-dir', type=Path, help='Optional directory for rendered bootstrap artifacts.')
    simulate_parser.add_argument('--format', choices=('text', 'json'), default='text')
    simulate_parser.set_defaults(func=_cmd_simulate)

    secrets_parser = subparsers.add_parser('secrets-check', help='Check that all secret refs exist locally.')
    _add_config_arg(secrets_parser)
    secrets_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    secrets_parser.set_defaults(func=_cmd_secrets_check)

    secret_set_parser = subparsers.add_parser('secret-set', help='Write one local secret ref with chmod 0600.')
    secret_set_parser.add_argument('ref', help='Secret ref, e.g. secrets/remnawave-api-key.')
    secret_set_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    secret_set_parser.add_argument('--stdin', action='store_true', help='Read secret value from stdin.')
    secret_set_parser.add_argument('--from-file', type=Path, help='Read secret value from a local file.')
    secret_set_parser.add_argument('--overwrite', action='store_true', help='Overwrite existing secret.')
    secret_set_parser.set_defaults(func=_cmd_secret_set)

    remnawave_check_parser = subparsers.add_parser('remnawave-check', help='Run read-only RemnaWave API contract checks.')
    remnawave_check_parser.add_argument('config', type=Path, nargs='?', help='Optional node config YAML for remnawave_api_url.')
    remnawave_check_parser.add_argument('--api-url', help='RemnaWave API URL; overrides config.main_server.remnawave_api_url.')
    remnawave_check_parser.add_argument('--api-key-ref', required=True, help='Secret ref containing RemnaWave API key.')
    remnawave_check_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    remnawave_check_parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
    remnawave_check_parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
    remnawave_check_parser.add_argument('--timeout-seconds', type=int, default=20)
    remnawave_check_parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
    remnawave_check_parser.add_argument('--format', choices=('text', 'json'), default='text')
    remnawave_check_parser.set_defaults(func=_cmd_remnawave_check)

    remnawave_keygen_parser = subparsers.add_parser(
        'remnawave-keygen',
        help='Fetch a RemnaWave Node SECRET_KEY and write it to the local secret store.',
    )
    remnawave_keygen_parser.add_argument('config', type=Path, help='Node config YAML whose remnanode.secret_key_ref should be written.')
    remnawave_keygen_parser.add_argument('--api-url', help='RemnaWave API URL; overrides config.main_server.remnawave_api_url.')
    remnawave_keygen_parser.add_argument('--api-key-ref', required=True, help='Secret ref containing RemnaWave API key.')
    remnawave_keygen_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    remnawave_keygen_parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
    remnawave_keygen_parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
    remnawave_keygen_parser.add_argument('--timeout-seconds', type=int, default=20)
    remnawave_keygen_parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
    remnawave_keygen_parser.add_argument('--overwrite', action='store_true', help='Overwrite existing SECRET_KEY ref.')
    remnawave_keygen_parser.set_defaults(func=_cmd_remnawave_keygen)

    cloudflare_check_parser = subparsers.add_parser('cloudflare-check', help='Run read-only Cloudflare zone checks.')
    cloudflare_check_parser.add_argument('domains', nargs='+', help='Cloudflare zone names to check.')
    cloudflare_check_parser.add_argument('--api-token-ref', required=True, help='Secret ref containing Cloudflare API token.')
    cloudflare_check_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    cloudflare_check_parser.add_argument('--timeout-seconds', type=int, default=20)
    cloudflare_check_parser.add_argument('--format', choices=('text', 'json'), default='text')
    cloudflare_check_parser.set_defaults(func=_cmd_cloudflare_check)

    dns_upsert_parser = subparsers.add_parser('dns-upsert', help='Upsert DNS-only Cloudflare A/AAAA records for a node config.')
    _add_config_arg(dns_upsert_parser)
    dns_upsert_parser.add_argument('--api-token-ref', help='Secret ref containing Cloudflare API token; defaults to site.dns_api_token_ref.')
    dns_upsert_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    dns_upsert_parser.add_argument('--timeout-seconds', type=int, default=20)
    dns_upsert_parser.add_argument('--proxied', action='store_true', help='Allow Cloudflare proxy. Do not use for VPN nodes in Russia.')
    dns_upsert_parser.add_argument('--format', choices=('text', 'json'), default='text')
    dns_upsert_parser.set_defaults(func=_cmd_dns_upsert)

    rotate_domain_parser = subparsers.add_parser('rotate-domain', help='Prepare a controlled node-domain rotation.')
    _add_config_arg(rotate_domain_parser)
    rotate_domain_parser.add_argument('--to', dest='to_domain', required=True, help='New node domain or subdomain.')
    rotate_domain_parser.add_argument('--output-config', type=Path, required=True, help='Where to write the rotated node YAML.')
    rotate_domain_parser.add_argument('--secrets-dir', type=Path, help='Local root for `secrets/...` refs.')
    rotate_domain_parser.add_argument('--state-dir', type=Path, help='Optional state root to record pending domain rotation.')
    rotate_domain_parser.add_argument('--api-token-ref', help='Cloudflare API token ref; defaults to rotated site.dns_api_token_ref.')
    rotate_domain_parser.add_argument('--upsert-dns', action='store_true', help='Create/update DNS-only A/AAAA records for the new domain.')
    rotate_domain_parser.add_argument(
        '--update-reality-secret',
        action='store_true',
        help='Update local REALITY secret serverNames for the new domain, keeping a backup file.',
    )
    rotate_domain_parser.add_argument(
        '--allow-custom-domain',
        action='store_true',
        help='Allow --to outside domain_rotation.spare_domains.',
    )
    rotate_domain_parser.add_argument('--timeout-seconds', type=int, default=20)
    rotate_domain_parser.add_argument('--format', choices=('text', 'json'), default='text')
    rotate_domain_parser.add_argument('--switch', action='store_true', help='After prepare, run the confirmed domain switch flow.')
    rotate_domain_parser.add_argument('--confirm-switch', action='store_true', help='Required with --switch to perform live/local writes.')
    rotate_domain_parser.add_argument('--switch-adapter', choices=('local', 'live'), default='local')
    rotate_domain_parser.add_argument('--env-dir', type=Path, help='Local fake environment root for --switch-adapter local.')
    rotate_domain_parser.add_argument('--render-dir', type=Path, help='Output root for rendered/bootstrap artifacts during switch.')
    rotate_domain_parser.add_argument('--api-url', help='RemnaWave API URL for --switch-adapter live; overrides config.main_server.remnawave_api_url.')
    rotate_domain_parser.add_argument('--api-key-ref', help='Secret ref containing RemnaWave API key for --switch-adapter live.')
    rotate_domain_parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
    rotate_domain_parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
    rotate_domain_parser.add_argument('--root-password-ref', help='Root password secret ref for live SSH bootstrap during switch.')
    rotate_domain_parser.add_argument('--root-private-key-ref', help='Root SSH private key secret ref for key-only live switch bootstrap.')
    rotate_domain_parser.add_argument('--admin-public-key-ref', default='secrets/ssh-admin-public-key')
    rotate_domain_parser.add_argument('--admin-private-key-ref', default='secrets/ssh-admin-private-key')
    rotate_domain_parser.add_argument('--dns-api-token-ref', help='DNS token ref for certificate issue; defaults to site.dns_api_token_ref.')
    rotate_domain_parser.add_argument('--acme-email', help='Email used for ACME registration; defaults to rotated site.contact_email.')
    rotate_domain_parser.add_argument('--no-cert-issue', action='store_true', help='Do not issue local-decoy Caddy certificates during switch bootstrap.')
    rotate_domain_parser.add_argument('--no-ssh-hardening', action='store_true', help='Do not disable password/root SSH after key verification.')
    rotate_domain_parser.add_argument('--no-start-services', action='store_true', help='Upload/install artifacts but do not start Docker/Caddy services.')
    rotate_domain_parser.add_argument('--no-resync-subscriptions', action='store_true', help='Update tariffs but do not sync active subscriptions.')
    rotate_domain_parser.set_defaults(func=_cmd_rotate_domain)

    change_sni_parser = subparsers.add_parser('change-sni', help='Change remote_dest REALITY target/SNI for a node.')
    _add_config_arg(change_sni_parser)
    change_sni_parser.add_argument('--target', required=True, help='New remote REALITY dest host:port, for example yahoo.com:443.')
    change_sni_parser.add_argument('--server-name', action='append', default=[], help='REALITY serverName/SNI. Defaults to target host.')
    change_sni_parser.add_argument('--output-config', type=Path, help='Where to write the changed YAML. Defaults to updating config in place.')
    change_sni_parser.add_argument('--secrets-dir', type=Path, default=DEFAULT_NODE_SECRETS_DIR)
    change_sni_parser.add_argument('--update-reality-secret', action='store_true', help='Update reality.credentials_ref serverNames.')
    change_sni_parser.add_argument('--apply-remnawave', action='store_true', help='Update RemnaWave config profile and host.')
    change_sni_parser.add_argument('--api-url', help='Override RemnaWave API URL.')
    change_sni_parser.add_argument('--api-key-ref', default=DEFAULT_REMNAWAVE_API_KEY_REF)
    change_sni_parser.add_argument('--auth-type', choices=('auto', 'api_key', 'bearer', 'caddy'), default='auto')
    change_sni_parser.add_argument('--caddy-token-ref', default=DEFAULT_REMNAWAVE_CADDY_TOKEN_REF)
    change_sni_parser.add_argument('--timeout-seconds', type=int, default=20)
    change_sni_parser.add_argument('--no-verify-tls', action='store_true')
    change_sni_parser.add_argument('--format', choices=('text', 'json'), default='text')
    change_sni_parser.set_defaults(func=_cmd_change_sni)

    availability_parser = subparsers.add_parser('availability-check', help='Run alert-only node availability checks from main/control-plane.')
    _add_config_arg(availability_parser)
    availability_parser.add_argument('--timeout-seconds', type=int, default=5)
    availability_parser.add_argument('--format', choices=('text', 'json'), default='text')
    availability_parser.set_defaults(func=_cmd_availability_check)

    ru_edge_check_parser = subparsers.add_parser('ru-edge-check', help='Run alert-only foreign-exit checks from a RU-edge SSH vantage point.')
    _add_config_arg(ru_edge_check_parser)
    ru_edge_check_parser.add_argument('--ru-edge-host', required=True, help='RU-edge public IP/host for SSH probe execution.')
    ru_edge_check_parser.add_argument('--ru-edge-user', default='templar', help='RU-edge SSH user.')
    ru_edge_check_parser.add_argument('--ru-edge-ssh-port', type=int, default=22, help='RU-edge SSH port.')
    ru_edge_check_parser.add_argument('--ru-edge-private-key-ref', required=True, help='Secret ref with RU-edge SSH private key.')
    ru_edge_check_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    ru_edge_check_parser.add_argument('--timeout-seconds', type=int, default=5)
    ru_edge_check_parser.add_argument('--format', choices=('text', 'json'), default='text')
    ru_edge_check_parser.set_defaults(func=_cmd_ru_edge_check)

    synthetic_parser = subparsers.add_parser('synthetic-vpn-check', help='Run a synthetic Xray client traffic/WARP check.')
    _add_config_arg(synthetic_parser)
    synthetic_parser.add_argument('--client-config', type=Path, required=True, help='Xray outbound/full client JSON exported for a test user.')
    synthetic_parser.add_argument('--xray-bin', default='xray')
    synthetic_parser.add_argument('--local-socks-port', type=int, default=18080)
    synthetic_parser.add_argument('--probe-url', default=DEFAULT_SYNTHETIC_PROBE_URL)
    synthetic_parser.add_argument('--expect-warp', dest='expect_warp', action='store_true', default=None)
    synthetic_parser.add_argument('--no-expect-warp', dest='expect_warp', action='store_false')
    synthetic_parser.add_argument('--timeout-seconds', type=int, default=20)
    synthetic_parser.add_argument('--format', choices=('text', 'json'), default='text')
    synthetic_parser.set_defaults(func=_cmd_synthetic_vpn_check)

    warp_register_parser = subparsers.add_parser(
        'warp-register',
        help='Register Cloudflare WARP and write config.warp.registration_ref.',
    )
    _add_config_arg(warp_register_parser)
    warp_register_parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    warp_register_parser.add_argument('--overwrite', action='store_true', help='Replace an existing WARP registration secret.')
    warp_register_parser.add_argument('--timeout-seconds', type=int, default=20)
    warp_register_parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
    _add_warp_register_args(warp_register_parser)
    warp_register_parser.set_defaults(func=_cmd_warp_register)

    state_show_parser = subparsers.add_parser('state-show', help='Show saved onboarding state for a node.')
    _add_config_arg(state_show_parser)
    _add_state_dir_arg(state_show_parser)
    state_show_parser.set_defaults(func=_cmd_state_show)

    state_init_parser = subparsers.add_parser('state-init', help='Create initial onboarding state for a node.')
    _add_config_arg(state_init_parser)
    _add_state_dir_arg(state_init_parser)
    state_init_parser.set_defaults(func=_cmd_state_init)

    state_mark_parser = subparsers.add_parser('state-mark', help='Mark a known checkpoint in local state.')
    _add_config_arg(state_mark_parser)
    state_mark_parser.add_argument('checkpoint', help='Known checkpoint/step id.')
    _add_state_dir_arg(state_mark_parser)
    state_mark_parser.set_defaults(func=_cmd_state_mark)

    cleanup_parser = subparsers.add_parser('cleanup-orphans', help='Show or clear orphaned object records from onboarding state.')
    _add_state_dir_arg(cleanup_parser)
    cleanup_parser.add_argument('--yes', action='store_true', help='Clear orphaned state records. Without this flag the command is read-only.')
    cleanup_parser.add_argument('--format', choices=('text', 'json'), default='text')
    cleanup_parser.set_defaults(func=_cmd_cleanup_orphans)

    decommission_parser = subparsers.add_parser(
        'delete',
        aliases=['decommission'],
        help='Delete a node connection and selected control-plane tails.',
    )
    decommission_parser.add_argument('config', type=Path, nargs='?', help='Path to node config YAML. Omit for interactive selection.')
    decommission_parser.add_argument('--config-dir', type=Path, action='append', default=[], help='Directory with node YAML configs for interactive selection.')
    decommission_parser.add_argument('--select', help='Select a discovered config by internal name, display name, domain, or file name.')
    decommission_parser.add_argument('--list', action='store_true', help='List discovered configs and exit.')
    decommission_parser.add_argument('--full', action='store_true', help='Enable full live cleanup defaults: RemnaWave, Bedolaga, DNS, local files, secrets, monitor, and remote VPS.')
    _add_state_dir_arg(decommission_parser)
    decommission_parser.add_argument('--adapter', choices=('none', 'local', 'http'), default='none', help='RemnaWave cleanup adapter. Defaults to dry-run local-only cleanup.')
    decommission_parser.add_argument('--bedolaga-adapter', choices=('auto', 'none', 'local', 'db', 'psql'), default='auto', help='Bedolaga cleanup adapter. auto uses local with --adapter local and none with --adapter http unless --full is set.')
    decommission_parser.add_argument('--bedolaga-db-container', default='remnawave_bot_db', help='Docker container with Postgres/psql for --bedolaga-adapter psql.')
    decommission_parser.add_argument('--env-dir', type=Path, help='Local fake environment root for local adapters.')
    decommission_parser.add_argument('--secrets-dir', type=Path, help='Local root for `secrets/...` refs.')
    decommission_parser.add_argument('--render-dir', type=Path, help='Render root whose <internal_name> directory can be removed.')
    decommission_parser.add_argument('--routes-file', type=Path, help='Route overrides YAML to remove this node from.')
    decommission_parser.add_argument('--monitor-config', type=Path, help='Node monitor YAML to remove checks from.')
    decommission_parser.add_argument('--delete-config-file', type=Path, help='Optional YAML config path to delete, usually the same path as CONFIG.')
    decommission_parser.add_argument('--delete-local-files', action='store_true', help='Remove state/render/routes/monitor/config files selected above.')
    decommission_parser.add_argument('--delete-secrets', action='store_true', help='Remove owned local secret refs for this node.')
    decommission_parser.add_argument('--delete-dns', action='store_true', help='Delete matching Cloudflare A/AAAA records for local_decoy_site domains.')
    decommission_parser.add_argument('--skip-dns-cleanup', action='store_true', help='With --full, do not delete Cloudflare DNS records.')
    decommission_parser.add_argument('--cloudflare-api-token-ref', help='Cloudflare API token ref; defaults to site.dns_api_token_ref.')
    decommission_parser.add_argument('--internal-squad-uuid', help='Override state-discovered Internal Squad UUID for Bedolaga cleanup.')
    decommission_parser.add_argument('--external-squad-uuid', help='Override state-discovered External Squad UUID for Bedolaga cleanup.')
    decommission_parser.add_argument('--delete-transit-service-user', action='store_true', help='Also delete the shared transit service user/credential. Use only when no RU-edge still uses it.')
    decommission_parser.add_argument('--ssh-cleanup', action='store_true', help='Clean dedicated VPS files/services/firewall over admin SSH.')
    decommission_parser.add_argument('--skip-ssh-cleanup', action='store_true', help='With --full, do not clean the remote VPS over SSH.')
    decommission_parser.add_argument('--disable-empty-monitor', action='store_true', help='Stop/disable templar-node-monitor.timer when the selected node was the last monitored check.')
    decommission_parser.add_argument('--admin-private-key-ref', help='Secret ref with admin SSH private key for --ssh-cleanup.')
    decommission_parser.add_argument('--root-password-ref', help='Fallback root password secret ref for --ssh-cleanup.')
    decommission_parser.add_argument('--api-url', help='RemnaWave API URL for --adapter http; overrides config.main_server.remnawave_api_url.')
    decommission_parser.add_argument('--api-key-ref', help='Secret ref containing RemnaWave API key for --adapter http.')
    decommission_parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
    decommission_parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
    decommission_parser.add_argument('--timeout-seconds', type=int, default=20)
    decommission_parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
    decommission_parser.add_argument('--yes', action='store_true', help='Apply deletions. Without this flag the command is a dry-run.')
    decommission_parser.add_argument('--format', choices=('text', 'json'), default='text')
    decommission_parser.set_defaults(func=_cmd_decommission)

    route_add_parser = subparsers.add_parser('route-add', help='Add RU-direct route overrides for a ru-edge node.')
    _add_config_arg(route_add_parser)
    route_add_parser.add_argument('--routes-file', type=Path, required=True)
    route_add_parser.add_argument('--domain', action='append', default=[], help='Domain or *.domain to route direct from RU.')
    route_add_parser.add_argument('--ip', action='append', default=[], help='IP or CIDR to route direct from RU.')
    route_add_parser.add_argument('--comment', help='Optional operator comment.')
    _add_route_apply_args(route_add_parser, include_apply=True, required_adapter=False, adapter_choices=('local', 'http'))
    route_add_parser.set_defaults(func=_cmd_route_add)

    route_apply_parser = subparsers.add_parser('route-apply', help='Apply RU-direct route overrides to a RemnaWave profile.')
    _add_config_arg(route_apply_parser)
    route_apply_parser.add_argument('--routes-file', type=Path, required=True)
    _add_route_apply_args(route_apply_parser, include_apply=False, required_adapter=True, adapter_choices=('local', 'http'))
    route_apply_parser.set_defaults(func=_cmd_route_apply)

    route_show_parser = subparsers.add_parser('route-show', help='Show route overrides for a node.')
    _add_config_arg(route_show_parser)
    route_show_parser.add_argument('--routes-file', type=Path, required=True)
    route_show_parser.set_defaults(func=_cmd_route_show)

    operator_parser = subparsers.add_parser('operator', help='Run one of the five high-level operator scenarios.')
    operator_subparsers = operator_parser.add_subparsers(dest='operator_scenario', required=True)
    operator_cascade_parser = operator_subparsers.add_parser(
        'cascade-direct',
        help='Scenario 1: configure FOREIGN-EXIT direct plus RU-EDGE cascade.',
    )
    operator_cascade_parser.add_argument('foreign_config', type=Path)
    operator_cascade_parser.add_argument('ru_edge_config', type=Path)
    _add_operator_common_args(operator_cascade_parser)
    operator_cascade_parser.add_argument('--foreign-root-password-ref', help='Root password secret ref for FOREIGN-EXIT VPS.')
    operator_cascade_parser.add_argument('--ru-root-password-ref', help='Root password secret ref for RU-EDGE VPS.')
    operator_cascade_parser.add_argument('--foreign-root-private-key-ref', help='Root SSH private key secret ref for key-only FOREIGN-EXIT VPS.')
    operator_cascade_parser.add_argument('--ru-root-private-key-ref', help='Root SSH private key secret ref for key-only RU-EDGE VPS.')
    operator_cascade_parser.set_defaults(func=_cmd_operator_cascade_direct)

    operator_ru_direct_parser = operator_subparsers.add_parser(
        'ru-direct',
        help='Scenario 2: configure RU direct-through-WARP with bought domain + decoy site.',
    )
    _add_config_arg(operator_ru_direct_parser)
    _add_operator_common_args(operator_ru_direct_parser)
    operator_ru_direct_parser.add_argument('--root-password-ref', help='Root password secret ref for RU VPS.')
    operator_ru_direct_parser.set_defaults(func=_cmd_operator_ru_direct)

    operator_routing_parser = operator_subparsers.add_parser(
        'routing-add',
        help='Scenario 3: add RU-direct routing overrides for a cascade node.',
    )
    _add_config_arg(operator_routing_parser)
    operator_routing_parser.add_argument('--routes-file', type=Path, required=True)
    operator_routing_parser.add_argument('--domain', action='append', default=[], help='Domain or *.domain to route direct from RU.')
    operator_routing_parser.add_argument('--ip', action='append', default=[], help='IP or CIDR to route direct from RU.')
    operator_routing_parser.add_argument('--comment', help='Optional operator comment.')
    _add_route_apply_args(
        operator_routing_parser,
        include_apply=True,
        required_adapter=False,
        adapter_choices=('local', 'live'),
        state_default=DEFAULT_STATE_DIR,
    )
    operator_routing_parser.set_defaults(func=_cmd_operator_routing_add)

    operator_ru_direct_remote_parser = operator_subparsers.add_parser(
        'ru-direct-remote',
        help='Scenario 4: configure RU direct-through-WARP without bought node-domain, using REALITY remote_dest.',
    )
    _add_config_arg(operator_ru_direct_remote_parser)
    _add_operator_common_args(operator_ru_direct_remote_parser)
    operator_ru_direct_remote_parser.add_argument('--root-password-ref', help='Root password secret ref for RU VPS.')
    operator_ru_direct_remote_parser.set_defaults(func=_cmd_operator_ru_direct_remote)

    operator_extra_ru_edge_parser = operator_subparsers.add_parser(
        'ru-edge-add',
        help='Scenario 5: add one more RU-EDGE to an existing FOREIGN-EXIT cascade.',
    )
    operator_extra_ru_edge_parser.add_argument('foreign_config', type=Path)
    operator_extra_ru_edge_parser.add_argument('ru_edge_config', type=Path)
    _add_operator_common_args(operator_extra_ru_edge_parser)
    operator_extra_ru_edge_parser.add_argument('--foreign-root-password-ref', help='Root password secret ref for FOREIGN-EXIT VPS.')
    operator_extra_ru_edge_parser.add_argument('--ru-root-password-ref', help='Root password secret ref for new RU-EDGE VPS.')
    operator_extra_ru_edge_parser.add_argument('--foreign-root-private-key-ref', help='Root SSH private key secret ref for key-only FOREIGN-EXIT VPS.')
    operator_extra_ru_edge_parser.add_argument('--ru-root-private-key-ref', help='Root SSH private key secret ref for key-only new RU-EDGE VPS.')
    operator_extra_ru_edge_parser.set_defaults(func=_cmd_operator_extra_ru_edge)

    for command in ('pre-bootstrap', 'bootstrap', 'post-bootstrap'):
        command_parser = subparsers.add_parser(command, help=f'Validate and plan the {command} phase.')
        _add_config_arg(command_parser)
        command_parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print plan only; do not perform SSH/API/DB writes.',
        )
        command_parser.add_argument('--secrets-dir', type=Path, help='Optional local root for `secrets/...` checks.')
        command_parser.add_argument('--state-dir', type=Path, help='Optional state root to display current checkpoint.')
        command_parser.add_argument('--adapter', choices=('local', 'ssh', 'db', 'http'), help='Adapter for real phase execution.')
        command_parser.add_argument('--env-dir', type=Path, help='Local fake environment root for adapter=local.')
        command_parser.add_argument('--render-dir', type=Path, help='Local output root for bootstrap artifacts.')
        command_parser.add_argument('--api-url', help='RemnaWave API URL; overrides config.main_server.remnawave_api_url.')
        command_parser.add_argument('--api-key-ref', help='Secret ref containing RemnaWave API key for adapter=http.')
        command_parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
        command_parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
        command_parser.add_argument('--timeout-seconds', type=int, default=20)
        command_parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
        command_parser.add_argument('--no-auto-warp-register', action='store_true', help='Do not create missing WARP registration secrets during live pre-bootstrap.')
        _add_warp_register_args(command_parser, include_format=False)
        command_parser.add_argument('--root-password-ref', help='Secret ref with initial root SSH password for adapter=ssh.')
        command_parser.add_argument('--root-private-key-ref', help='Secret ref with root SSH private key for key-only fresh VPSes.')
        command_parser.add_argument('--admin-public-key-ref', default='secrets/ssh-admin-public-key')
        command_parser.add_argument('--admin-private-key-ref', default='secrets/ssh-admin-private-key')
        command_parser.add_argument('--dns-api-token-ref', help='Optional DNS token ref for external ACME DNS-01 certificate issue.')
        command_parser.add_argument('--acme-email', help='Email used for ACME registration; defaults to site.contact_email.')
        command_parser.add_argument('--no-cert-issue', action='store_true', help='Do not issue local-decoy Caddy certificates during SSH bootstrap.')
        command_parser.add_argument('--no-ssh-hardening', action='store_true', help='Do not disable password/root SSH after key verification.')
        command_parser.add_argument('--no-start-services', action='store_true', help='Upload/install artifacts but do not start Docker/Caddy services.')
        command_parser.add_argument('--no-resync-subscriptions', action='store_true', help='For post-bootstrap adapter=db: update tariffs but do not sync active subscriptions to RemnaWave.')
        command_parser.add_argument('--continue-from', choices=LAYER2B_CHECKPOINTS, help='For post-bootstrap recovery, resume from a known Layer 2b checkpoint.')
        command_parser.set_defaults(func=_cmd_phase_stub, phase=command)

    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('config', type=Path, help='Path to node config YAML.')


def _add_state_dir_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR, help='Control-plane node state root.')


def _add_generation_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--main-ipv4', required=True)
    parser.add_argument('--remnawave-api-url', required=True)
    parser.add_argument('--admin-allowlist', action='append', required=True)
    parser.add_argument('--admin-user', default='templar')
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--dns-api-token-ref', default='secrets/dns-api-token')


def _add_tariff_args(parser: argparse.ArgumentParser, *, prefix: str) -> None:
    option_prefix = f'--{prefix}-' if prefix else '--'
    dest_prefix = f'{prefix}_' if prefix else ''
    parser.add_argument(f'{option_prefix}tariff-slug', action='append', default=[], dest=f'{dest_prefix}tariff_slugs')
    parser.add_argument(f'{option_prefix}tariff-name', action='append', default=[], dest=f'{dest_prefix}tariff_names')
    parser.add_argument(
        f'{option_prefix}trial-eligible',
        action='store_true',
        default=False,
        dest=f'{dest_prefix}trial_eligible',
        help='Make this node available for free trial subscriptions.',
    )


def _add_node_generation_args(
    parser: argparse.ArgumentParser,
    *,
    prefix: str,
    country_default: str | None,
    domain_required: bool = True,
) -> None:
    option_prefix = f'--{prefix}-' if prefix else '--'
    dest_prefix = f'{prefix}_' if prefix else ''
    parser.add_argument(f'{option_prefix}internal-name', required=True, dest=f'{dest_prefix}internal_name')
    parser.add_argument(f'{option_prefix}display-name', required=True, dest=f'{dest_prefix}display_name')
    parser.add_argument(f'{option_prefix}domain', required=domain_required, dest=f'{dest_prefix}domain')
    parser.add_argument(f'{option_prefix}ipv4', required=True, dest=f'{dest_prefix}ipv4')
    parser.add_argument(f'{option_prefix}ipv6', dest=f'{dest_prefix}ipv6')
    parser.add_argument(f'{option_prefix}spare-domain', action='append', default=[], dest=f'{dest_prefix}spare_domains')
    country_kwargs: dict[str, object] = {'dest': f'{dest_prefix}country_code'}
    if country_default is None:
        country_kwargs['required'] = True
    else:
        country_kwargs['default'] = country_default
    parser.add_argument(f'{option_prefix}country-code', **country_kwargs)


def _add_ru_warp_reality_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--reality-strategy',
        choices=('local_decoy_site', 'remote_dest'),
        default='remote_dest',
        help='local_decoy_site uses a bought domain + local Caddy site; remote_dest uses server IP + SNI target.',
    )
    parser.add_argument(
        '--reality-target',
        default=DEFAULT_REMOTE_DEST_TARGET,
        help='Remote REALITY target for --reality-strategy remote_dest, e.g. ya.ru:443.',
    )
    parser.add_argument(
        '--reality-server-name',
        action='append',
        default=[],
        dest='reality_server_names',
        help='REALITY serverName/SNI for remote_dest. Defaults to the target host when omitted.',
    )


def _add_foreign_reality_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--foreign-reality-strategy',
        choices=('local_decoy_site', 'remote_dest'),
        default='remote_dest',
        help='foreign-exit REALITY strategy. remote_dest uses server IP + SNI target without local site.',
    )
    parser.add_argument(
        '--foreign-reality-target',
        default=DEFAULT_REMOTE_DEST_TARGET,
        help='Foreign remote REALITY target for --foreign-reality-strategy remote_dest, e.g. ya.ru:443.',
    )
    parser.add_argument(
        '--foreign-reality-server-name',
        action='append',
        default=[],
        dest='foreign_reality_server_names',
        help='Foreign REALITY serverName/SNI for remote_dest. Defaults to the target host when omitted.',
    )


def _add_ru_edge_reality_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--ru-reality-strategy',
        choices=('local_decoy_site', 'remote_dest'),
        default='remote_dest',
        help='RU-edge REALITY strategy. remote_dest makes RU use IP + SNI target without local site.',
    )
    parser.add_argument(
        '--ru-reality-target',
        default=DEFAULT_REMOTE_DEST_TARGET,
        help='Remote REALITY target for --ru-reality-strategy remote_dest, e.g. ya.ru:443.',
    )
    parser.add_argument(
        '--ru-reality-server-name',
        action='append',
        default=[],
        dest='ru_reality_server_names',
        help='RU-edge REALITY serverName/SNI for remote_dest. Defaults to the target host when omitted.',
    )


def _add_route_apply_args(
    parser: argparse.ArgumentParser,
    *,
    include_apply: bool,
    required_adapter: bool,
    adapter_choices: tuple[str, ...],
    state_default: Path | None = None,
) -> None:
    if include_apply:
        parser.add_argument('--apply', action='store_true', help='Push route overrides into the RemnaWave Config Profile.')
    parser.add_argument('--adapter', choices=adapter_choices, required=required_adapter, help='Adapter used with route apply.')
    parser.add_argument('--env-dir', type=Path, help='Local fake environment root for --adapter local.')
    parser.add_argument('--secrets-dir', type=Path, help='Local root for `secrets/...` refs used by live/http apply.')
    parser.add_argument('--state-dir', type=Path, default=state_default, help='Control-plane node state root for config_profile_uuid lookup.')
    parser.add_argument('--api-url', help='RemnaWave API URL; overrides config.main_server.remnawave_api_url.')
    parser.add_argument('--api-key-ref', help='Secret ref containing RemnaWave API key for live/http apply.')
    parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
    parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
    parser.add_argument('--timeout-seconds', type=int, default=20)
    parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')


def _add_warp_register_args(parser: argparse.ArgumentParser, *, include_format: bool = True) -> None:
    parser.add_argument('--warp-api-base-url', default='https://api.cloudflareclient.com')
    parser.add_argument('--warp-api-version', default='v0a2483')
    parser.add_argument('--warp-client-version', default='a-6.81-2410012252.0')
    parser.add_argument('--warp-user-agent', default='1.1.1.1/6.81')
    parser.add_argument('--warp-device-model', default='VPN Node')
    parser.add_argument('--warp-license-key-ref', help='Optional secret ref with Cloudflare WARP+ license key.')
    if include_format:
        parser.add_argument('--format', choices=('text', 'json'), default='text')


def _add_operator_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--adapter', choices=('local', 'live'), default='local')
    parser.add_argument('--secrets-dir', type=Path, required=True, help='Local root for `secrets/...` refs.')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR, help='Control-plane node state root.')
    parser.add_argument('--render-dir', type=Path, required=True, help='Local output root for rendered bootstrap artifacts.')
    parser.add_argument('--env-dir', type=Path, help='Local fake environment root for --adapter local.')
    parser.add_argument('--api-key-ref', help='Secret ref containing RemnaWave API key for --adapter live.')
    parser.add_argument('--auth-type', choices=('api_key', 'bearer', 'caddy'), default='api_key')
    parser.add_argument('--caddy-token-ref', help='Optional secret ref for Caddy Security X-Api-Key token.')
    parser.add_argument('--cloudflare-api-token-ref', help='Optional DNS token ref; defaults to site.dns_api_token_ref.')
    parser.add_argument('--skip-dns', action='store_true', help='Do not upsert Cloudflare DNS records in live mode.')
    parser.add_argument('--timeout-seconds', type=int, default=20)
    parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
    parser.add_argument('--no-auto-warp-register', action='store_true', help='Do not create missing WARP registration secrets in live mode.')
    _add_warp_register_args(parser, include_format=False)
    parser.add_argument('--admin-public-key-ref', default='secrets/ssh-admin-public-key')
    parser.add_argument('--admin-private-key-ref', default='secrets/ssh-admin-private-key')
    parser.add_argument('--root-private-key-ref', help='Secret ref with a root SSH private key for key-only fresh VPSes.')
    parser.add_argument('--acme-email', help='Email used for ACME registration; defaults to site.contact_email.')
    parser.add_argument('--no-cert-issue', action='store_true', help='Do not issue local-decoy Caddy certificates during SSH bootstrap.')
    parser.add_argument('--no-ssh-hardening', action='store_true', help='Do not disable password/root SSH after key verification.')
    parser.add_argument('--no-start-services', action='store_true', help='Upload/install artifacts but do not start Docker/Caddy services.')
    parser.add_argument('--no-resync-subscriptions', action='store_true', help='Update tariffs but do not sync active subscriptions.')


def _add_quick_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--adapter', choices=('local', 'live'), default='live', help='Provisioning adapter. Short aliases use live by default.')
    parser.add_argument('--work-root', type=Path, default=DEFAULT_QUICK_WORK_ROOT, help='Root for generated configs/state/render/routes.')
    parser.add_argument('--configs-dir', type=Path, help='Directory for generated and discovered node YAML configs.')
    parser.add_argument('--config-dir', type=Path, action='append', default=[], help='Extra directory to search when a selection menu is needed.')
    parser.add_argument('--secrets-dir', type=Path, help='Local root for `secrets/...` refs. Defaults to /opt/templar/secrets.')
    parser.add_argument('--state-dir', type=Path, help='Control-plane node state root.')
    parser.add_argument('--render-dir', type=Path, help='Local output root for rendered bootstrap artifacts.')
    parser.add_argument('--routes-file', type=Path, help='Route overrides YAML path.')
    parser.add_argument('--env-dir', type=Path, help='Local fake environment root for --adapter local.')
    parser.add_argument('--main-ipv4', default=DEFAULT_QUICK_MAIN_IPV4)
    parser.add_argument('--remnawave-api-url', default=DEFAULT_QUICK_REMNAWAVE_API_URL)
    parser.add_argument('--admin-allowlist', action='append', default=[])
    parser.add_argument('--admin-user', default='templar')
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--dns-api-token-ref', default='secrets/dns-api-token')
    parser.add_argument('--api-key-ref', default=DEFAULT_REMNAWAVE_API_KEY_REF)
    parser.add_argument('--auth-type', choices=('auto', 'api_key', 'bearer', 'caddy'), default='auto')
    parser.add_argument('--caddy-token-ref', default=DEFAULT_REMNAWAVE_CADDY_TOKEN_REF)
    parser.add_argument('--cloudflare-api-token-ref', help='Optional DNS token ref; defaults to site.dns_api_token_ref.')
    parser.add_argument('--skip-dns', action='store_true', help='Do not upsert Cloudflare DNS records in live mode.')
    parser.add_argument('--timeout-seconds', type=int, default=20)
    parser.add_argument('--no-verify-tls', action='store_true', help='Disable TLS verification for local testing only.')
    parser.add_argument('--no-auto-warp-register', action='store_true', help='Do not create missing WARP registration secrets in live mode.')
    _add_warp_register_args(parser, include_format=False)
    parser.add_argument('--admin-public-key-ref', default='secrets/ssh-admin-public-key')
    parser.add_argument('--admin-private-key-ref', default='secrets/ssh-admin-private-key')
    parser.add_argument('--root-private-key-ref', help='Secret ref with a root SSH private key for key-only fresh VPSes.')
    parser.add_argument('--acme-email', help='Email used for ACME registration; defaults to site.contact_email.')
    parser.add_argument('--no-cert-issue', action='store_true', help='Do not issue local-decoy Caddy certificates during SSH bootstrap.')
    parser.add_argument('--no-ssh-hardening', action='store_true', help='Do not disable password/root SSH after key verification.')
    parser.add_argument('--no-start-services', action='store_true', help='Upload/install artifacts but do not start Docker/Caddy services.')
    parser.add_argument('--no-resync-subscriptions', action='store_true', help='Update tariffs but do not sync active subscriptions.')
    parser.add_argument('--generate-only', action='store_true', help='Only generate YAML and write root password refs; do not run onboarding.')


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    print(f'OK: {config.display.internal_name} ({config.role.value})')
    print(f'Domain: {config.domain}')
    print(f'Display: {config.display.name}')
    print(f'Secret refs: {len(config.secret_refs())}')
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    plan = build_plan(config)
    if args.format == 'json':
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        print('\n'.join(plan.to_lines()))
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    secret_store = LocalSecretStore(args.secrets_dir) if args.secrets_dir else None
    bundle = render_bundle(config, secret_store=secret_store)
    written = write_bundle(bundle, args.output_dir)
    print(f'Rendered artifacts: {len(written)}')
    for path in written:
        print(path)
    return 0


def _cmd_generate_cascade_direct(args: argparse.Namespace) -> int:
    try:
        generated = generate_cascade_direct(
            CascadeDirectInput(
                common=_common_generation_input(args),
                foreign=_node_generation_input(args, 'foreign'),
                ru_edge=_node_generation_input(args, 'ru'),
                foreign_tariffs=_tariffs_from_args(args, 'foreign'),
                ru_edge_tariffs=_tariffs_from_args(args, 'ru'),
                service_user=args.service_user,
                foreign_reality_strategy=args.foreign_reality_strategy,
                foreign_remote_dest_target=args.foreign_reality_target,
                foreign_remote_dest_server_names=tuple(args.foreign_reality_server_names),
                ru_edge_reality_strategy=args.ru_reality_strategy,
                ru_edge_remote_dest_target=args.ru_reality_target,
                ru_edge_remote_dest_server_names=tuple(args.ru_reality_server_names),
            ),
        )
        written = write_generated_configs(generated, args.output_dir)
    except ConfigBuildError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_written_configs(written)
    return 0


def _cmd_generate_extra_ru_edge(args: argparse.Namespace) -> int:
    try:
        foreign_config = load_node_config(args.foreign_config)
        generated = generate_extra_ru_edge(
            ExtraRuEdgeInput(
                foreign_config=foreign_config,
                ru_edge=_node_generation_input(args, ''),
                tariffs=_tariffs_from_args(args, ''),
                reality_strategy=args.reality_strategy,
                remote_dest_target=args.reality_target,
                remote_dest_server_names=tuple(args.reality_server_names),
            ),
        )
        written = write_generated_configs(generated, args.output_dir)
        if args.updated_foreign_config is not None:
            updated_foreign = build_foreign_config_with_extra_ru_edge(foreign_config, args.ipv4, args.ipv6)
            written.append(write_yaml_config(updated_foreign, args.updated_foreign_config))
    except (ConfigBuildError, NodeConfigLoadError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_written_configs(written)
    return 0


def _cmd_generate_ru_warp(args: argparse.Namespace) -> int:
    try:
        generated = generate_ru_warp(
            RuWarpInput(
                common=_common_generation_input(args),
                node=_node_generation_input(args, ''),
                tariffs=_tariffs_from_args(args, ''),
                reality_strategy=args.reality_strategy,
                remote_dest_target=args.reality_target,
                remote_dest_server_names=tuple(args.reality_server_names),
            ),
        )
        written = write_generated_configs(generated, args.output_dir)
    except ConfigBuildError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_written_configs(written)
    return 0


def _cmd_wizard_cascade_direct(args: argparse.Namespace) -> int:
    print('Node wizard: cascade + foreign direct')
    common = _prompt_common_generation_input()
    foreign_reality_strategy = _prompt_choice(
        'Foreign exit REALITY strategy',
        choices=('local_decoy_site', 'remote_dest'),
        default='remote_dest',
    )
    foreign = _prompt_node_generation_input(
        'Foreign exit',
        country_default=None,
        require_domain=foreign_reality_strategy == 'local_decoy_site',
    )
    foreign_remote_dest_target = DEFAULT_REMOTE_DEST_TARGET
    foreign_remote_dest_server_names = (DEFAULT_REMOTE_DEST_SERVER_NAME,)
    if foreign_reality_strategy == 'remote_dest':
        foreign_remote_dest_target = _prompt('Foreign remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
        foreign_remote_dest_server_names = tuple(
            _prompt_csv(
                'Foreign remote REALITY serverNames',
                required=False,
                default=_default_server_name_for_target(foreign_remote_dest_target),
            ),
        )
    ru_edge_reality_strategy = _prompt_choice(
        'RU edge REALITY strategy',
        choices=('local_decoy_site', 'remote_dest'),
        default='remote_dest',
    )
    ru_edge = _prompt_node_generation_input(
        'RU edge',
        country_default='RU',
        require_domain=ru_edge_reality_strategy == 'local_decoy_site',
    )
    ru_edge_remote_dest_target = DEFAULT_REMOTE_DEST_TARGET
    ru_edge_remote_dest_server_names = (DEFAULT_REMOTE_DEST_SERVER_NAME,)
    if ru_edge_reality_strategy == 'remote_dest':
        ru_edge_remote_dest_target = _prompt('RU edge remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
        ru_edge_remote_dest_server_names = tuple(
            _prompt_csv(
                'RU edge remote REALITY serverNames',
                required=False,
                default=_default_server_name_for_target(ru_edge_remote_dest_target),
            ),
        )
    foreign_tariffs = _prompt_tariffs('Foreign direct tariffs')
    ru_edge_tariffs = _prompt_tariffs('RU cascade tariffs')
    service_user = _prompt('Transit service user', default='bridge_ru_to_foreign')
    try:
        generated = generate_cascade_direct(
            CascadeDirectInput(
                common=common,
                foreign=foreign,
                ru_edge=ru_edge,
                foreign_tariffs=foreign_tariffs,
                ru_edge_tariffs=ru_edge_tariffs,
                service_user=service_user,
                foreign_reality_strategy=foreign_reality_strategy,
                foreign_remote_dest_target=foreign_remote_dest_target,
                foreign_remote_dest_server_names=foreign_remote_dest_server_names,
                ru_edge_reality_strategy=ru_edge_reality_strategy,
                ru_edge_remote_dest_target=ru_edge_remote_dest_target,
                ru_edge_remote_dest_server_names=ru_edge_remote_dest_server_names,
            ),
        )
        written = write_generated_configs(generated, args.output_dir)
    except ConfigBuildError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_written_configs(written)
    return 0


def _cmd_wizard_ru_warp(args: argparse.Namespace) -> int:
    print('Node wizard: RU WARP')
    common = _prompt_common_generation_input()
    reality_strategy = _prompt_choice(
        'REALITY strategy',
        choices=('local_decoy_site', 'remote_dest'),
        default='remote_dest',
    )
    node = _prompt_node_generation_input(
        'RU WARP node',
        country_default='RU',
        require_domain=reality_strategy == 'local_decoy_site',
    )
    remote_dest_target = DEFAULT_REMOTE_DEST_TARGET
    remote_dest_server_names = (DEFAULT_REMOTE_DEST_SERVER_NAME,)
    if reality_strategy == 'remote_dest':
        remote_dest_target = _prompt('Remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
        remote_dest_server_names = tuple(
            _prompt_csv(
                'Remote REALITY serverNames',
                required=False,
                default=_default_server_name_for_target(remote_dest_target),
            ),
        )
    tariffs = _prompt_tariffs('RU WARP tariffs')
    try:
        generated = generate_ru_warp(
            RuWarpInput(
                common=common,
                node=node,
                tariffs=tariffs,
                reality_strategy=reality_strategy,
                remote_dest_target=remote_dest_target,
                remote_dest_server_names=remote_dest_server_names,
            ),
        )
        written = write_generated_configs(generated, args.output_dir)
    except ConfigBuildError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_written_configs(written)
    return 0


def _cmd_quick_cascade_direct(args: argparse.Namespace) -> int:
    paths = _quick_paths(args)
    print('Quick scenario: cascade + foreign direct without local sites')
    try:
        secret_store = LocalSecretStore(paths.secrets_dir)
        common = _quick_common_generation_input(args)
        foreign = _prompt_quick_node_generation_input('Foreign direct / foreign exit', country_default='LV', require_domain=False)
        ru_edge = _prompt_quick_node_generation_input('RU cascade edge', country_default='RU', require_domain=False)
        foreign_root_ref, foreign_root_key_ref = _quick_root_ssh_auth_refs(
            args,
            paths,
            secret_store,
            'Foreign server',
            foreign.internal_name,
            live=args.adapter == 'live',
        )
        ru_root_ref, ru_root_key_ref = _quick_root_ssh_auth_refs(
            args,
            paths,
            secret_store,
            'RU server',
            ru_edge.internal_name,
            live=args.adapter == 'live',
        )
        foreign_tariffs = _prompt_quick_tariffs('Foreign direct tariffs', default_preset='7')
        ru_edge_tariffs = _prompt_quick_tariffs('RU cascade tariffs', default_preset='7')
        service_user = _prompt('Transit service user', default='bridge_ru_to_foreign')
        foreign_remote_dest_target = _prompt('Foreign remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
        foreign_remote_dest_server_names = tuple(
            _prompt_csv(
                'Foreign remote REALITY serverNames',
                required=False,
                default=_default_server_name_for_target(foreign_remote_dest_target),
            ),
        )
        ru_edge_remote_dest_target = _prompt('RU edge remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
        ru_edge_remote_dest_server_names = tuple(
            _prompt_csv(
                'RU edge remote REALITY serverNames',
                required=False,
                default=_default_server_name_for_target(ru_edge_remote_dest_target),
            ),
        )
        generated = generate_cascade_direct(
            CascadeDirectInput(
                common=common,
                foreign=foreign,
                ru_edge=ru_edge,
                foreign_tariffs=foreign_tariffs,
                ru_edge_tariffs=ru_edge_tariffs,
                service_user=service_user,
                foreign_reality_strategy='remote_dest',
                foreign_remote_dest_target=foreign_remote_dest_target,
                foreign_remote_dest_server_names=foreign_remote_dest_server_names,
                ru_edge_reality_strategy='remote_dest',
                ru_edge_remote_dest_target=ru_edge_remote_dest_target,
                ru_edge_remote_dest_server_names=ru_edge_remote_dest_server_names,
            ),
        )
        written = write_generated_configs(generated, paths.configs_dir)
        _print_written_configs(written)
        if args.generate_only:
            _print_quick_paths(paths)
            return 0
        result = run_cascade_direct_operator(
            foreign_config=load_node_config(written[0]),
            ru_edge_config=load_node_config(written[1]),
            context=_quick_operator_context(args, paths, allow_admin_ssh_bootstrap=True),
            foreign_root_password_ref=foreign_root_ref,
            ru_root_password_ref=ru_root_ref,
            foreign_root_private_key_ref=foreign_root_key_ref,
            ru_root_private_key_ref=ru_root_key_ref,
        )
    except (ConfigBuildError, NodeConfigLoadError, OperatorError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_quick_paths(paths)
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_quick_direct_site(args: argparse.Namespace) -> int:
    return _cmd_quick_direct_common(args, reality_strategy='local_decoy_site')


def _cmd_quick_direct_remote(args: argparse.Namespace) -> int:
    return _cmd_quick_direct_common(args, reality_strategy='remote_dest')


def _cmd_quick_direct_common(args: argparse.Namespace, *, reality_strategy: str) -> int:
    paths = _quick_paths(args)
    title = 'direct with site masking' if reality_strategy == 'local_decoy_site' else 'direct without a local site'
    print(f'Quick scenario: {title}')
    try:
        secret_store = LocalSecretStore(paths.secrets_dir)
        common = _quick_common_generation_input(args)
        node = _prompt_quick_node_generation_input(
            'RU direct WARP node',
            country_default='RU',
            require_domain=reality_strategy == 'local_decoy_site',
        )
        root_ref, root_key_ref = _quick_root_ssh_auth_refs(
            args,
            paths,
            secret_store,
            'RU server',
            node.internal_name,
            live=args.adapter == 'live',
        )
        tariffs = _prompt_quick_tariffs('Direct tariffs', default_preset='7')
        remote_dest_target = DEFAULT_REMOTE_DEST_TARGET
        remote_dest_server_names = (DEFAULT_REMOTE_DEST_SERVER_NAME,)
        if reality_strategy == 'remote_dest':
            remote_dest_target = _prompt('Remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
            remote_dest_server_names = tuple(
                _prompt_csv(
                    'Remote REALITY serverNames',
                    required=False,
                    default=_default_server_name_for_target(remote_dest_target),
                ),
            )
        generated = generate_ru_warp(
            RuWarpInput(
                common=common,
                node=node,
                tariffs=tariffs,
                reality_strategy=reality_strategy,
                remote_dest_target=remote_dest_target,
                remote_dest_server_names=remote_dest_server_names,
            ),
        )
        written = write_generated_configs(generated, paths.configs_dir)
        _print_written_configs(written)
        if args.generate_only:
            _print_quick_paths(paths)
            return 0
        result = run_ru_direct_operator(
            config=load_node_config(written[0]),
            context=_quick_operator_context(args, paths, allow_admin_ssh_bootstrap=True),
            root_password_ref=root_ref,
            root_private_key_ref=root_key_ref,
            require_remote_dest=reality_strategy == 'remote_dest',
        )
    except (ConfigBuildError, NodeConfigLoadError, OperatorError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_quick_paths(paths)
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_quick_extra_ru_edge(args: argparse.Namespace) -> int:
    paths = _quick_paths(args)
    print('Quick scenario: add RU edge to an existing foreign exit without a local site')
    try:
        secret_store = LocalSecretStore(paths.secrets_dir)
        foreign_path = args.foreign_config or _quick_select_config_path(
            paths,
            args.config_dir,
            label='Select foreign exit',
            roles=('foreign-exit',),
        )
        foreign_config = load_node_config(foreign_path)
        ru_edge = _prompt_quick_node_generation_input('New RU cascade edge', country_default='RU', require_domain=False)
        foreign_root_ref, foreign_root_key_ref = _quick_existing_node_root_ssh_auth_refs(
            args,
            paths,
            secret_store,
            'Foreign server',
            foreign_config,
            live=args.adapter == 'live',
        )
        ru_root_ref, ru_root_key_ref = _quick_root_ssh_auth_refs(
            args,
            paths,
            secret_store,
            'New RU server',
            ru_edge.internal_name,
            live=args.adapter == 'live',
        )
        tariffs = _prompt_quick_tariffs('RU cascade tariffs', default_preset='7')
        remote_dest_target = _prompt('RU edge remote REALITY dest host:port', default=DEFAULT_REMOTE_DEST_TARGET)
        remote_dest_server_names = tuple(
            _prompt_csv(
                'RU edge remote REALITY serverNames',
                required=False,
                default=_default_server_name_for_target(remote_dest_target),
            ),
        )
        generated = generate_extra_ru_edge(
            ExtraRuEdgeInput(
                foreign_config=foreign_config,
                ru_edge=ru_edge,
                tariffs=tariffs,
                reality_strategy='remote_dest',
                remote_dest_target=remote_dest_target,
                remote_dest_server_names=remote_dest_server_names,
            ),
        )
        written = write_generated_configs(generated, paths.configs_dir)
        updated_foreign_raw = build_foreign_config_with_extra_ru_edge(foreign_config, ru_edge.public_ipv4, ru_edge.public_ipv6)
        _backup_existing_config(foreign_path)
        write_yaml_config(updated_foreign_raw, foreign_path)
        written.insert(0, Path(foreign_path))
        _print_written_configs(written)
        if args.generate_only:
            _print_quick_paths(paths)
            return 0
        result = run_extra_ru_edge_operator(
            foreign_config=load_node_config(foreign_path),
            ru_edge_config=load_node_config(written[1]),
            context=_quick_operator_context(args, paths, allow_admin_ssh_bootstrap=True),
            foreign_root_password_ref=foreign_root_ref,
            ru_root_password_ref=ru_root_ref,
            foreign_root_private_key_ref=foreign_root_key_ref,
            ru_root_private_key_ref=ru_root_key_ref,
        )
    except (ConfigBuildError, NodeConfigLoadError, OperatorError, SecretStoreError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_quick_paths(paths)
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_quick_routing_add(args: argparse.Namespace) -> int:
    paths = _quick_paths(args)
    print('Quick scenario: add routing rules')
    try:
        config_path = args.config or _quick_select_config_path(
            paths,
            args.config_dir,
            label='Select RU edge',
            roles=('ru-edge',),
        )
        config = load_node_config(config_path)
        domains = _prompt_csv('Domains to route directly from RU', required=False)
        ips = _prompt_csv('IP/CIDR to route directly from RU', required=False)
        if not domains and not ips:
            raise ValueError('At least one domain or IP/CIDR is required')
        comment = _prompt_optional('Comment')
        if args.no_apply:
            result = run_routing_add_operator(
                config=config,
                routes_file=paths.routes_file,
                domains=domains,
                ips=ips,
                comment=comment,
                apply=False,
            )
        else:
            result = run_routing_add_operator(
                config=config,
                routes_file=paths.routes_file,
                domains=domains,
                ips=ips,
                comment=comment,
                apply=True,
                remnawave_adapter=_quick_routing_adapter(args, paths, config),
                state_store=NodeStateStore(paths.state_dir),
            )
    except (NodeConfigLoadError, OperatorError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_quick_paths(paths)
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_quick_sni_change(args: argparse.Namespace) -> int:
    paths = _quick_paths(args)
    print('Quick scenario: change remote_dest REALITY SNI')
    try:
        config_path = args.config or _quick_select_config_path(
            paths,
            args.config_dir,
            label='Select remote_dest node',
            roles=(),
        )
        config = load_node_config(config_path)
        current_target = config.reality.target or DEFAULT_REMOTE_DEST_TARGET
        target = args.target or _prompt('New remote REALITY dest host:port', default=current_target)
        server_names = tuple(args.server_name or _prompt_csv(
            'New remote REALITY serverNames',
            required=False,
            default=_default_server_name_for_target(target),
        ))
        auth_type, caddy_token_ref = _quick_resolved_auth(args, paths.secrets_dir)
        result = _run_sni_change(
            config,
            config_path=config_path,
            output_config=config_path,
            target=target,
            server_names=server_names,
            secrets_dir=paths.secrets_dir,
            update_reality_secret=True,
            apply_remnawave=not args.no_apply,
            adapter_args=argparse.Namespace(
                adapter=args.adapter,
                env_dir=paths.env_dir,
                api_url=None,
                api_key_ref=args.api_key_ref,
                auth_type=auth_type,
                caddy_token_ref=caddy_token_ref,
                timeout_seconds=args.timeout_seconds,
                no_verify_tls=args.no_verify_tls,
            ),
        )
    except (DomainRotationError, NodeConfigLoadError, SecretStoreError, RemnaWaveAdapterError, RemnaWaveProbeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print_quick_paths(paths)
    print('\n'.join(result['lines']))
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    configs = [load_node_config(path) for path in args.configs]
    results = []
    try:
        env_store = FakeEnvironmentStore(args.env_dir)
        state_store = NodeStateStore(args.state_dir)
        for config in configs:
            results.append(
                simulate_onboarding(
                    config,
                    env_store=env_store,
                    state_store=state_store,
                    render_dir=args.render_dir,
                ),
            )
    except SimulationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == 'json':
        payload = [result.to_dict() for result in results]
        print(json.dumps(payload[0] if len(payload) == 1 else payload, ensure_ascii=False, indent=2))
    else:
        rendered = []
        for result in results:
            rendered.extend(result.to_lines())
            rendered.append('')
        if rendered:
            rendered.pop()
        print('\n'.join(rendered))
    return 0


def _cmd_route_add(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        result = RouteOverrideStore(args.routes_file).add(
            config,
            domains=args.domain,
            ips=args.ip,
            comment=args.comment,
        )
        lines = result.to_lines()
        if args.apply:
            apply_result = _apply_routing_from_args(args, config)
            lines.extend(['', *apply_result.to_lines()])
    except (RouteOverrideError, OperatorError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(lines))
    return 0


def _cmd_route_apply(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        result = _apply_routing_from_args(args, config)
    except (OperatorError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_route_show(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        routes = RouteOverrideStore(args.routes_file).load()
    except RouteOverrideError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    node_routes = routes.get('nodes', {}).get(config.display.internal_name, {})
    print(yaml.safe_dump(node_routes, sort_keys=False, allow_unicode=True))
    return 0


def _cmd_operator_cascade_direct(args: argparse.Namespace) -> int:
    try:
        result = run_cascade_direct_operator(
            foreign_config=load_node_config(args.foreign_config),
            ru_edge_config=load_node_config(args.ru_edge_config),
            context=_operator_context(args),
            foreign_root_password_ref=args.foreign_root_password_ref,
            ru_root_password_ref=args.ru_root_password_ref,
            foreign_root_private_key_ref=args.foreign_root_private_key_ref,
            ru_root_private_key_ref=args.ru_root_private_key_ref,
        )
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_operator_extra_ru_edge(args: argparse.Namespace) -> int:
    try:
        result = run_extra_ru_edge_operator(
            foreign_config=load_node_config(args.foreign_config),
            ru_edge_config=load_node_config(args.ru_edge_config),
            context=_operator_context(args),
            foreign_root_password_ref=args.foreign_root_password_ref,
            ru_root_password_ref=args.ru_root_password_ref,
            foreign_root_private_key_ref=args.foreign_root_private_key_ref,
            ru_root_private_key_ref=args.ru_root_private_key_ref,
        )
    except (NodeConfigLoadError, OperatorError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_operator_ru_direct(args: argparse.Namespace) -> int:
    return _cmd_operator_ru_direct_common(args, require_remote_dest=False)


def _cmd_operator_ru_direct_remote(args: argparse.Namespace) -> int:
    return _cmd_operator_ru_direct_common(args, require_remote_dest=True)


def _cmd_operator_ru_direct_common(args: argparse.Namespace, *, require_remote_dest: bool) -> int:
    try:
        result = run_ru_direct_operator(
            config=load_node_config(args.config),
            context=_operator_context(args),
            root_password_ref=args.root_password_ref,
            root_private_key_ref=args.root_private_key_ref,
            require_remote_dest=require_remote_dest,
        )
    except OperatorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_operator_routing_add(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        result = run_routing_add_operator(
            config=config,
            routes_file=args.routes_file,
            domains=args.domain,
            ips=args.ip,
            comment=args.comment,
            apply=args.apply,
            remnawave_adapter=_routing_apply_adapter(args, config) if args.apply else None,
            state_store=NodeStateStore(args.state_dir) if args.apply and args.state_dir else None,
        )
    except (OperatorError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_secrets_check(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    summary = LocalSecretStore(args.secrets_dir).check_refs(config.secret_refs())
    print('\n'.join(summary.to_lines()))
    return 0 if summary.ok else 1


def _cmd_secret_set(args: argparse.Namespace) -> int:
    try:
        value = _read_secret_input(args)
        if not value:
            print('secret value cannot be empty', file=sys.stderr)
            return 2
        path = LocalSecretStore(args.secrets_dir).write_text(args.ref, value, overwrite=args.overwrite)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f'Secret written: {args.ref}')
    print(f'Path: {path}')
    return 0


def _cmd_remnawave_check(args: argparse.Namespace) -> int:
    try:
        api_url = _resolve_remnawave_api_url(args)
        secret_store = LocalSecretStore(args.secrets_dir)
        api_key = secret_store.read_text(args.api_key_ref)
        caddy_token = secret_store.read_text(args.caddy_token_ref) if args.caddy_token_ref else None
        result = run_remnawave_probe(
            api_url=api_url,
            auth=RemnaWaveProbeAuth(api_key=api_key, auth_type=args.auth_type, caddy_token=caddy_token),
            timeout_seconds=args.timeout_seconds,
            verify_tls=not args.no_verify_tls,
        )
    except (RemnaWaveProbeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == 'json':
        print(result.to_json())
    else:
        print('\n'.join(result.to_lines()))
    return 0


def _cmd_remnawave_keygen(args: argparse.Namespace) -> int:
    try:
        config = load_node_config(args.config)
        api_url = _resolve_remnawave_api_url(args)
        secret_store = LocalSecretStore(args.secrets_dir)
        api_key = secret_store.read_text(args.api_key_ref)
        caddy_token = secret_store.read_text(args.caddy_token_ref) if args.caddy_token_ref else None
        value = fetch_remnawave_node_secret_key(
            api_url=api_url,
            auth=RemnaWaveProbeAuth(api_key=api_key, auth_type=args.auth_type, caddy_token=caddy_token),
            timeout_seconds=args.timeout_seconds,
            verify_tls=not args.no_verify_tls,
        )
        path = secret_store.write_text(config.remnanode.secret_key_ref, value, overwrite=args.overwrite)
    except (RemnaWaveProbeError, SecretStoreError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f'RemnaWave Node SECRET_KEY written: {config.remnanode.secret_key_ref}')
    print(f'Path: {path}')
    return 0


def _cmd_cloudflare_check(args: argparse.Namespace) -> int:
    try:
        api_token = LocalSecretStore(args.secrets_dir).read_text(args.api_token_ref)
        result = run_cloudflare_check(
            domains=list(args.domains),
            api_token=api_token,
            timeout_seconds=args.timeout_seconds,
        )
    except CloudflareCheckError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == 'json':
        print(result.to_json())
    else:
        print('\n'.join(result.to_lines()))
    return 0 if result.ok else 1


def _cmd_dns_upsert(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    api_token_ref = args.api_token_ref or config.site.dns_api_token_ref
    if not api_token_ref:
        print('dns-upsert requires --api-token-ref or site.dns_api_token_ref', file=sys.stderr)
        return 2
    if args.proxied:
        print('Refusing --proxied for node DNS records; keep Cloudflare DNS-only for VPN nodes.', file=sys.stderr)
        return 2
    try:
        api_token = LocalSecretStore(args.secrets_dir).read_text(api_token_ref)
        result = upsert_node_dns_records(
            fqdn=config.domain,
            ipv4=config.public_ipv4,
            ipv6=_public_dns_ipv6(config),
            ttl=config.domain_rotation.dns_ttl_seconds,
            api_token=api_token,
            timeout_seconds=args.timeout_seconds,
            proxied=False,
        )
    except (CloudflareUpsertError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == 'json':
        print(result.to_json())
    else:
        print('\n'.join(result.to_lines()))
    return 0 if result.ok else 1


def _cmd_rotate_domain(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        plan = build_domain_rotation_plan(
            config,
            to_domain=args.to_domain,
            allow_custom_domain=args.allow_custom_domain,
        )
        config_path = write_rotated_config(plan.rotated_config, args.output_config)
        secret_store = LocalSecretStore(args.secrets_dir) if args.secrets_dir else None
        dns_records = None
        reality_secret = None

        if args.upsert_dns:
            if secret_store is None:
                raise DomainRotationError('--upsert-dns requires --secrets-dir')
            api_token_ref = args.api_token_ref or plan.rotated_config.site.dns_api_token_ref
            if not api_token_ref:
                raise DomainRotationError('--upsert-dns requires --api-token-ref or site.dns_api_token_ref')
            dns_result = upsert_node_dns_records(
                fqdn=plan.new_domain,
                ipv4=plan.rotated_config.public_ipv4,
                ipv6=_public_dns_ipv6(plan.rotated_config),
                ttl=plan.rotated_config.domain_rotation.dns_ttl_seconds,
                api_token=secret_store.read_text(api_token_ref),
                timeout_seconds=args.timeout_seconds,
                proxied=False,
            )
            dns_records = [record.to_dict() for record in dns_result.records]

        if args.update_reality_secret:
            if secret_store is None:
                raise DomainRotationError('--update-reality-secret requires --secrets-dir')
            if not plan.rotated_config.reality.credentials_ref:
                raise DomainRotationError('rotated config has no reality.credentials_ref')
            reality_secret = rotate_reality_secret_server_names(
                secret_store,
                plan.rotated_config.reality.credentials_ref,
                server_names=plan.rotated_config.effective_reality_server_names(),
            )

        state_path = None
        switch_payload = None
        if args.state_dir:
            state_path = save_domain_rotation_state(
                NodeStateStore(args.state_dir),
                plan,
                config_path=config_path,
                old_config_path=args.config,
                dns_records=dns_records,
                reality_secret=reality_secret,
            )
        if args.switch:
            switch_payload = _run_domain_rotation_switch(
                args,
                plan=plan,
                config_path=config_path,
                secret_store=secret_store,
            )
            state_path = Path(switch_payload['state']['path'])
    except (DomainRotationError, CloudflareUpsertError, SecretStoreError, Layer1Error, Layer2bError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == 'json':
        print(
            json.dumps(
                {
                    'plan': plan.to_dict(config_path=config_path),
                    'dns_records': dns_records or [],
                    'reality_secret': reality_secret.to_dict() if reality_secret else None,
                    'state_path': str(state_path) if state_path else None,
                    'switch': switch_payload,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
    else:
        lines = plan.to_lines(config_path=config_path)
        if dns_records is not None:
            lines.append(f'DNS records prepared: {len(dns_records)}')
            lines.extend(
                f'- {record["status"]} {record["record_type"]} {record["name"]} -> {record["content"]}'
                for record in dns_records
            )
        if reality_secret is not None:
            lines.extend(reality_secret.to_lines())
        if state_path is not None:
            lines.append(f'State: {state_path}')
        if switch_payload is None:
            lines.append('Next: run rotate-domain --switch --confirm-switch to issue the certificate, update RemnaWave, and resync Bedolaga.')
        else:
            lines.append(f'Domain switch: {switch_payload["state"]["status"]}')
            lines.extend(switch_payload['state_lines'])
        print('\n'.join(lines))
    return 0


def _cmd_change_sni(args: argparse.Namespace) -> int:
    config_path = args.config
    config = load_node_config(config_path)
    try:
        payload = _run_sni_change(
            config,
            config_path=config_path,
            output_config=args.output_config or config_path,
            target=args.target,
            server_names=tuple(args.server_name or ()),
            secrets_dir=args.secrets_dir,
            update_reality_secret=args.update_reality_secret,
            apply_remnawave=args.apply_remnawave,
            adapter_args=args,
        )
    except (DomainRotationError, SecretStoreError, RemnaWaveAdapterError, RemnaWaveProbeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('\n'.join(payload['lines']))
    return 0


def _run_sni_change(
    config,
    *,
    config_path: Path,
    output_config: Path,
    target: str,
    server_names: tuple[str, ...],
    secrets_dir: Path,
    update_reality_secret: bool,
    apply_remnawave: bool,
    adapter_args: argparse.Namespace,
) -> dict[str, object]:
    plan = build_sni_change_plan(config, target=target, server_names=server_names)
    output_path = Path(output_config).expanduser().resolve()
    source_path = Path(config_path).expanduser().resolve()
    backup_path = None
    if output_path == source_path:
        backup = _backup_existing_config(source_path, label='Config backup')
        backup_path = str(backup) if backup else None
    written_path = write_sni_changed_config(plan.changed_config, output_path)

    secret_store = LocalSecretStore(secrets_dir)
    reality_secret = None
    if update_reality_secret:
        if not plan.changed_config.reality.credentials_ref:
            raise DomainRotationError('changed config has no reality.credentials_ref')
        reality_secret = rotate_reality_secret_server_names(
            secret_store,
            plan.changed_config.reality.credentials_ref,
            server_names=list(plan.changed_config.effective_reality_server_names()),
        )

    remnawave_updates: list[str] = []
    if apply_remnawave:
        adapter = _sni_change_remnawave_adapter(adapter_args, plan.changed_config, secret_store)
        _status_line(f'{plan.changed_config.display.internal_name}: updating RemnaWave config profile')
        profile = adapter.ensure_config_profile(plan.changed_config)
        remnawave_updates.append(f'Config profile: {profile.status} {profile.uuid}')
        _status_line(f'{plan.changed_config.display.internal_name}: updating RemnaWave host')
        host = adapter.ensure_host(plan.changed_config)
        remnawave_updates.append(f'Host: {host.status} {host.uuid}')

    lines = plan.to_lines(config_path=written_path)
    if backup_path:
        lines.append(f'Backup: {backup_path}')
    if reality_secret is not None:
        lines.extend(reality_secret.to_lines())
    if remnawave_updates:
        lines.append('RemnaWave updates:')
        lines.extend(f'- {line}' for line in remnawave_updates)
    if not apply_remnawave:
        lines.append('RemnaWave not changed; rerun with --apply-remnawave or use change_sni quick flow.')

    return {
        'plan': plan.to_dict(config_path=written_path),
        'backup_path': backup_path,
        'reality_secret': reality_secret.to_dict() if reality_secret else None,
        'remnawave_updates': remnawave_updates,
        'lines': lines,
    }


def _sni_change_remnawave_adapter(args: argparse.Namespace, config, secret_store: LocalSecretStore):
    if getattr(args, 'adapter', None) == 'local':
        return LocalRemnaWaveAdapter(FakeEnvironmentStore(args.env_dir), secret_store=secret_store)
    auth_type = args.auth_type
    caddy_token_ref = args.caddy_token_ref
    if auth_type == 'auto':
        auth_type, caddy_token_ref = _quick_resolved_auth(args, secret_store.root_dir)
    adapter_args = argparse.Namespace(
        api_url=getattr(args, 'api_url', None),
        api_key_ref=args.api_key_ref,
        auth_type=auth_type,
        caddy_token_ref=caddy_token_ref,
        timeout_seconds=args.timeout_seconds,
        no_verify_tls=args.no_verify_tls,
    )
    return _http_remnawave_adapter(adapter_args, config, secret_store)


def _run_domain_rotation_switch(
    args: argparse.Namespace,
    *,
    plan,
    config_path: Path,
    secret_store: LocalSecretStore | None,
) -> dict[str, object]:
    if not args.confirm_switch:
        raise DomainRotationError('--switch requires --confirm-switch')
    if secret_store is None:
        raise DomainRotationError('--switch requires --secrets-dir')
    if args.state_dir is None:
        raise DomainRotationError('--switch requires --state-dir')
    if args.render_dir is None:
        raise DomainRotationError('--switch requires --render-dir')

    state_store = NodeStateStore(args.state_dir)
    mark_domain_rotation_switch_started(state_store, plan, config_path=config_path)
    bootstrap_lines: list[str] = []
    post_bootstrap_lines: list[str] = []
    try:
        if args.switch_adapter == 'local':
            if args.env_dir is None:
                raise DomainRotationError('--switch-adapter local requires --env-dir')
            env_store = FakeEnvironmentStore(args.env_dir)
            bootstrap = run_layer1_local_bootstrap(
                plan.rotated_config,
                secret_store=secret_store,
                state_store=state_store,
                output_dir=args.render_dir,
                env_store=env_store,
                progress=_status_line,
            )
            post_bootstrap = run_layer2b_post_bootstrap(
                plan.rotated_config,
                remnawave_adapter=LocalRemnaWaveAdapter(env_store, secret_store=secret_store),
                bedolaga_adapter=LocalBedolagaAdapter(env_store),
                state_store=state_store,
                progress=_status_line,
            )
        else:
            missing = []
            if not args.root_password_ref and not args.root_private_key_ref and not args.admin_private_key_ref:
                missing.append('--root-password-ref, --root-private-key-ref or --admin-private-key-ref')
            if not args.api_key_ref:
                missing.append('--api-key-ref')
            if missing:
                raise DomainRotationError(f'--switch-adapter live requires {", ".join(missing)}')
            bootstrap = _run_layer1_ssh_with_admin_retry(
                plan.rotated_config,
                secret_store=secret_store,
                state_store=state_store,
                output_dir=args.render_dir,
                args=args,
            )
            post_bootstrap = run_layer2b_post_bootstrap(
                plan.rotated_config,
                remnawave_adapter=_http_remnawave_adapter(args, plan.rotated_config, secret_store),
                bedolaga_adapter=DatabaseBedolagaAdapter(resync_subscriptions=not args.no_resync_subscriptions),
                state_store=state_store,
                progress=_status_line,
                node_online_timeout_seconds=120,
                node_online_interval_seconds=10,
            )
        bootstrap_lines = bootstrap.to_lines()
        post_bootstrap_lines = post_bootstrap.to_lines()
        switch_state = mark_domain_rotation_switch_succeeded(state_store, plan, config_path=config_path)
    except (DomainRotationError, Layer1Error, Layer2bError, SecretStoreError, ValueError) as exc:
        switch_state = mark_domain_rotation_switch_failed(
            state_store,
            plan,
            config_path=config_path,
            error=str(exc),
        )
        raise DomainRotationError(f'domain rotation switch failed: {exc}') from exc

    return {
        'state': switch_state.to_dict(),
        'state_lines': switch_state.to_lines(),
        'bootstrap': bootstrap_lines,
        'post_bootstrap': post_bootstrap_lines,
    }


def _cmd_availability_check(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    result = run_main_availability_check(config, timeout_seconds=args.timeout_seconds)
    if args.format == 'json':
        print(result.to_json())
    else:
        print('\n'.join(result.to_lines()))
    return 0 if result.ok else 1


def _cmd_ru_edge_check(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        private_key = LocalSecretStore(args.secrets_dir).read_text(args.ru_edge_private_key_ref)
    except SecretStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result = run_ru_edge_foreign_exit_check(
        config,
        runner=SshRemoteCommandRunner(
            host=args.ru_edge_host,
            user=args.ru_edge_user,
            port=args.ru_edge_ssh_port,
            private_key=private_key,
        ),
        timeout_seconds=args.timeout_seconds,
    )
    if args.format == 'json':
        print(result.to_json())
    else:
        print('\n'.join(result.to_lines()))
    return 0 if result.ok else 1


def _cmd_synthetic_vpn_check(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    result = run_synthetic_vpn_check(
        config,
        client_config_path=args.client_config,
        xray_bin=args.xray_bin,
        local_socks_port=args.local_socks_port,
        probe_url=args.probe_url,
        expect_warp=args.expect_warp,
        timeout_seconds=args.timeout_seconds,
    )
    if args.format == 'json':
        print(result.to_json())
    else:
        print('\n'.join(result.to_lines()))
    return 0 if result.ok else 1


def _cmd_warp_register(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        secret_store = LocalSecretStore(args.secrets_dir)
        result = ensure_warp_registration_for_config(
            config,
            secret_store=secret_store,
            options=_warp_registration_options(args, secret_store),
            overwrite=args.overwrite,
        )
    except (WarpRegistrationError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if result is None:
        print(f'WARP registration skipped: {config.display.internal_name} has warp.mode={config.warp.mode.value}')
        return 0
    if args.format == 'json':
        print(
            json.dumps(
                {
                    'ref': result.ref,
                    'path': str(result.path),
                    'status': result.status,
                    'device_id': result.device_id,
                    'endpoint': result.endpoint,
                    'address': list(result.address),
                    'reserved': list(result.reserved),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
    else:
        print('\n'.join(result.to_lines()))
    return 0


def _cmd_state_show(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    state = NodeStateStore(args.state_dir).load(config.display.internal_name)
    if state is None:
        print(f'No state for {config.display.internal_name} in {args.state_dir}')
        return 1
    print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_state_init(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    store = NodeStateStore(args.state_dir)
    state = store.load_or_init(config)
    path = store.save(state)
    print(f'Initialized state: {path}')
    return 0


def _cmd_state_mark(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    try:
        state = NodeStateStore(args.state_dir).mark_checkpoint(config, args.checkpoint)
    except StateStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f'Marked checkpoint: {state.last_completed_step}')
    return 0


def _cmd_cleanup_orphans(args: argparse.Namespace) -> int:
    store = NodeStateStore(args.state_dir)
    try:
        states = store.iter_states()
    except StateStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    records = [
        {
            'state_path': str(path),
            'internal_name': state.internal_name,
            'orphaned': state.orphaned,
        }
        for path, state in states
        if state.orphaned
    ]
    if args.format == 'json':
        print(json.dumps({'cleared': bool(args.yes), 'records': records}, ensure_ascii=False, indent=2, sort_keys=True))
    elif not records:
        print('No orphaned records found.')
    else:
        print('Orphaned records:')
        for record in records:
            print(f"- {record['internal_name']}: {len(record['orphaned'])} record(s) in {record['state_path']}")
        if not args.yes:
            print('Dry run only. Re-run with --yes to clear orphaned records from state.')
    if not args.yes:
        return 0
    cleared = 0
    try:
        for _, state in states:
            if not state.orphaned:
                continue
            cleared += state.clear_orphaned()
            store.save(state)
    except StateStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == 'text':
        print(f'Cleared orphaned records: {cleared}')
    return 0


def _cmd_decommission(args: argparse.Namespace) -> int:
    try:
        if args.list:
            records = _discover_decommission_configs(args.config_dir)
            _print_decommission_candidates(records)
            return 0 if records else 1
        config_path = _resolve_decommission_config_path(args)
        config = load_node_config(config_path)
        if args.full:
            _apply_decommission_full_defaults(args, config_path, config)
        secret_store = LocalSecretStore(args.secrets_dir) if args.secrets_dir else None
        result = run_decommission(
            config,
            state_store=NodeStateStore(args.state_dir),
            remnawave_adapter=_decommission_remnawave_adapter(args, config, secret_store),
            bedolaga_adapter=_decommission_bedolaga_adapter(args),
            secret_store=secret_store,
            render_dir=args.render_dir,
            routes_file=args.routes_file,
            monitor_config=args.monitor_config,
            config_file=config_path,
            delete_dns=args.delete_dns,
            cloudflare_api_token_ref=args.cloudflare_api_token_ref,
            delete_local_files=args.delete_local_files,
            delete_config_file=args.delete_config_file,
            delete_secrets=args.delete_secrets,
            delete_transit_service_user=args.delete_transit_service_user,
            ssh_cleanup=args.ssh_cleanup,
            ssh_private_key_ref=args.admin_private_key_ref,
            ssh_root_password_ref=args.root_password_ref,
            disable_empty_monitor=args.disable_empty_monitor,
            internal_squad_uuid_override=args.internal_squad_uuid,
            external_squad_uuid_override=args.external_squad_uuid,
            dry_run=not args.yes,
            ssh_timeout_seconds=args.timeout_seconds,
        )
    except (DecommissionError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.format == 'json':
        print(
            json.dumps(
                {
                    'internal_name': result.internal_name,
                    'dry_run': result.dry_run,
                    'lines': result.to_lines(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
    else:
        print('\n'.join(result.to_lines()))
    return 0


def _resolve_decommission_config_path(args: argparse.Namespace) -> Path:
    if args.config is not None:
        return Path(args.config)
    records = _discover_decommission_configs(args.config_dir)
    if not records:
        raise ValueError('No node configs found. Pass CONFIG or --config-dir.')
    if args.select:
        return _select_decommission_record(records, args.select)['path']
    if not sys.stdin.isatty():
        if len(records) == 1:
            return records[0]['path']
        raise ValueError('Pass CONFIG or --select when stdin is not interactive.')
    _print_decommission_candidates(records)
    while True:
        answer = input(f'Select server to delete [1-{len(records)}]: ').strip()
        if not answer:
            continue
        try:
            index = int(answer)
        except ValueError:
            print('Enter a number from the list.', file=sys.stderr)
            continue
        if 1 <= index <= len(records):
            return records[index - 1]['path']
        print('Selected number is out of range.', file=sys.stderr)


def _discover_decommission_configs(config_dirs: list[Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[Path] = set()
    for directory in _decommission_config_dirs(config_dirs):
        if not directory.exists():
            continue
        for path in sorted([*directory.glob('*.yml'), *directory.glob('*.yaml')]):
            resolved = path.expanduser().resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                config = load_node_config(resolved)
            except NodeConfigLoadError:
                continue
            records.append({'path': resolved, 'config': config})
    records.sort(key=lambda item: str(item['config'].display.internal_name))
    return records


def _decommission_config_dirs(config_dirs: list[Path]) -> list[Path]:
    raw_dirs = config_dirs or [Path.cwd() / 'configs', *DEFAULT_DECOMMISSION_CONFIG_DIRS]
    directories: list[Path] = []
    seen: set[Path] = set()
    for directory in raw_dirs:
        resolved = Path(directory).expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        directories.append(resolved)
    return directories


def _select_decommission_record(records: list[dict[str, object]], selector: str) -> dict[str, object]:
    selector_normalized = selector.strip().lower()
    matches = [record for record in records if selector_normalized in _decommission_record_keys(record)]
    if not matches:
        raise ValueError(f'No node config matches --select {selector!r}')
    if len(matches) > 1:
        names = ', '.join(str(record['config'].display.internal_name) for record in matches)
        raise ValueError(f'--select {selector!r} matched multiple configs: {names}')
    return matches[0]


def _decommission_record_keys(record: dict[str, object]) -> set[str]:
    config = record['config']
    path = Path(record['path'])
    return {
        config.display.internal_name.lower(),
        config.display.name.lower(),
        config.domain.lower(),
        config.domain.split('.', 1)[0].lower(),
        config.public_ipv4.lower(),
        config.country_code.lower(),
        path.name.lower(),
        path.stem.lower(),
        str(path).lower(),
    }


def _print_decommission_candidates(records: list[dict[str, object]]) -> None:
    if not records:
        print('No node configs found.')
        return
    print('Available node configs:')
    for index, record in enumerate(records, start=1):
        config = record['config']
        path = record['path']
        print(
            f'{index}. {config.display.internal_name} | {config.display.name} | '
            f'{config.role.value} | {config.country_code} | {config.domain} | {config.public_ipv4} | {path}',
        )


def _apply_decommission_full_defaults(args: argparse.Namespace, config_path: Path, config) -> None:
    work_root = _infer_node_work_root(config_path)
    if args.adapter == 'none':
        args.adapter = 'http'
    if args.bedolaga_adapter == 'auto':
        args.bedolaga_adapter = 'local' if args.adapter == 'local' else 'psql'
    if args.state_dir == DEFAULT_STATE_DIR and work_root is not None:
        args.state_dir = work_root / 'state'
    if args.secrets_dir is None:
        args.secrets_dir = _default_decommission_secrets_dir(work_root)
    if args.render_dir is None and work_root is not None:
        args.render_dir = work_root / 'render'
    if args.routes_file is None and work_root is not None and (work_root / 'routes.yml').exists():
        args.routes_file = work_root / 'routes.yml'
    if args.monitor_config is None and work_root is not None and (work_root / 'monitor' / 'monitor.yml').exists():
        args.monitor_config = work_root / 'monitor' / 'monitor.yml'
    if args.delete_config_file is None:
        args.delete_config_file = config_path
    args.delete_local_files = True
    args.delete_secrets = True
    args.delete_transit_service_user = bool(args.delete_transit_service_user or config.role != NodeRole.RU_EDGE)
    args.disable_empty_monitor = True
    if not args.skip_dns_cleanup:
        args.delete_dns = True
    if not args.skip_ssh_cleanup:
        args.ssh_cleanup = True
    if args.adapter == 'http' and not args.api_key_ref:
        args.api_key_ref = DEFAULT_REMNAWAVE_API_KEY_REF
    if args.secrets_dir is not None and not args.caddy_token_ref and _secret_ref_exists(args.secrets_dir, DEFAULT_REMNAWAVE_CADDY_TOKEN_REF):
        args.caddy_token_ref = DEFAULT_REMNAWAVE_CADDY_TOKEN_REF
        args.auth_type = 'caddy'
    if not args.admin_private_key_ref:
        args.admin_private_key_ref = DEFAULT_ADMIN_PRIVATE_KEY_REF
    if args.secrets_dir is not None and not args.root_password_ref and not getattr(args, 'root_private_key_ref', None):
        args.root_password_ref = _default_root_password_ref(args.secrets_dir, config)


def _infer_node_work_root(config_path: Path) -> Path | None:
    resolved = Path(config_path).expanduser().resolve()
    if resolved.parent.name == 'configs':
        return resolved.parent.parent
    return None


def _default_decommission_secrets_dir(work_root: Path | None) -> Path | None:
    if work_root is not None and (work_root / 'secrets').exists():
        return work_root / 'secrets'
    if DEFAULT_NODE_SECRETS_DIR.exists():
        return DEFAULT_NODE_SECRETS_DIR
    return None


def _default_root_password_ref(secrets_dir: Path, config) -> str | None:
    candidates = []
    first_domain_label = config.domain.split('.', 1)[0]
    candidates.append(f'secrets/ssh-root-password-{_slug(first_domain_label)}')
    candidates.append(f'secrets/ssh-root-password-{_slug(config.display.internal_name)}')
    candidates.append(f'secrets/ssh-root-password-{_slug(config.display.name)}')
    for ref in dict.fromkeys(candidates):
        if _secret_ref_exists(secrets_dir, ref):
            return ref
    return None


def _secret_ref_exists(secrets_dir: Path, ref: str) -> bool:
    try:
        return LocalSecretStore(secrets_dir).path_for_ref(ref).is_file()
    except SecretStoreError:
        return False


def _slug(value: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', value.strip().lower()).strip('-')
    return slug or 'node'


def _cmd_phase_stub(args: argparse.Namespace) -> int:
    config = load_node_config(args.config)
    if not args.dry_run:
        if args.phase == 'pre-bootstrap':
            return _cmd_pre_bootstrap_real(args, config)
        if args.phase == 'bootstrap':
            return _cmd_bootstrap_real(args, config)
        if args.phase == 'post-bootstrap':
            return _cmd_post_bootstrap_real(args, config)
        print(
            f'{args.phase} is not implemented for real writes yet. '
            f'Run `templar-node {args.phase} --dry-run {args.config}` for the current safe mode.',
            file=sys.stderr,
        )
        return 2
    plan = build_plan(config)
    print(f'DRY RUN: {args.phase}')
    if args.state_dir:
        _print_state_summary(config.display.internal_name, args.state_dir)
    if args.secrets_dir:
        summary = LocalSecretStore(args.secrets_dir).check_refs(config.secret_refs())
        print('\n'.join(summary.to_lines()))
        print()
    print('\n'.join(plan.to_lines()))
    return 0


def _run_layer1_ssh_with_admin_retry(
    config,
    *,
    secret_store: LocalSecretStore,
    state_store: NodeStateStore,
    output_dir: Path,
    args: argparse.Namespace,
):
    def run_with(root_password_ref: str | None, root_private_key_ref: str | None):
        return run_layer1_ssh_bootstrap(
            config,
            secret_store=secret_store,
            state_store=state_store,
            output_dir=output_dir,
            options=Layer1SshBootstrapOptions(
                root_password_ref=root_password_ref,
                root_private_key_ref=root_private_key_ref,
                admin_public_key_ref=args.admin_public_key_ref,
                admin_private_key_ref=args.admin_private_key_ref,
                dns_api_token_ref=args.dns_api_token_ref,
                acme_email=args.acme_email,
                issue_certificates=not args.no_cert_issue,
                harden_ssh=not args.no_ssh_hardening,
                start_services=not args.no_start_services,
                progress=_status_line,
            ),
        )

    try:
        return run_with(args.root_password_ref, args.root_private_key_ref)
    except Layer1Error as exc:
        if (args.root_password_ref or args.root_private_key_ref) and args.admin_private_key_ref:
            _status_line(f'{config.display.internal_name}: root SSH failed; retrying bootstrap with existing admin SSH key')
            try:
                return run_with(None, None)
            except Layer1Error as retry_exc:
                raise Layer1Error(f'{exc}; admin-key retry failed: {retry_exc}') from retry_exc
        raise


def _cmd_post_bootstrap_real(args: argparse.Namespace, config) -> int:
    if args.adapter == 'db':
        return _cmd_post_bootstrap_db(args, config)
    if args.adapter == 'http':
        return _cmd_post_bootstrap_http(args, config)
    missing = []
    if args.adapter != 'local':
        missing.append('--adapter local')
    if args.env_dir is None:
        missing.append('--env-dir')
    if args.state_dir is None:
        missing.append('--state-dir')
    if missing:
        print(f'post-bootstrap requires {", ".join(missing)} for the current safe adapter mode', file=sys.stderr)
        return 2
    env_store = FakeEnvironmentStore(args.env_dir)
    try:
        result = run_layer2b_post_bootstrap(
            config,
            remnawave_adapter=LocalRemnaWaveAdapter(env_store),
            bedolaga_adapter=LocalBedolagaAdapter(env_store),
            state_store=NodeStateStore(args.state_dir),
            progress=_status_line,
            node_online_timeout_seconds=120,
            node_online_interval_seconds=10,
        )
    except Layer2bError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_post_bootstrap_http(args: argparse.Namespace, config) -> int:
    missing = []
    if args.secrets_dir is None:
        missing.append('--secrets-dir')
    if args.state_dir is None:
        missing.append('--state-dir')
    if not args.api_key_ref:
        missing.append('--api-key-ref')
    if missing:
        print(f'post-bootstrap --adapter http requires {", ".join(missing)}', file=sys.stderr)
        return 2
    try:
        secret_store = LocalSecretStore(args.secrets_dir)
        result = run_layer2b_post_bootstrap(
            config,
            remnawave_adapter=_http_remnawave_adapter(args, config, secret_store),
            bedolaga_adapter=DatabaseBedolagaAdapter(resync_subscriptions=not args.no_resync_subscriptions),
            state_store=NodeStateStore(args.state_dir),
            progress=_status_line,
            node_online_timeout_seconds=120,
            node_online_interval_seconds=10,
        )
    except (Layer2bError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_post_bootstrap_db(args: argparse.Namespace, config) -> int:
    missing = []
    if args.state_dir is None:
        missing.append('--state-dir')
    if missing:
        print(f'post-bootstrap --adapter db requires {", ".join(missing)}', file=sys.stderr)
        return 2
    state_store = NodeStateStore(args.state_dir)
    state = state_store.load(config.display.internal_name)
    if state is None:
        print('post-bootstrap --adapter db requires existing state with discovered RemnaWave UUIDs', file=sys.stderr)
        return 2
    if args.continue_from and args.continue_from != 'bedolaga_pending':
        print('post-bootstrap --adapter db currently supports --continue-from bedolaga_pending only', file=sys.stderr)
        return 2
    if args.continue_from == 'bedolaga_pending' and 'remnawave_config_ok' not in state.checkpoints:
        print('--continue-from bedolaga_pending requires checkpoint remnawave_config_ok in state', file=sys.stderr)
        return 2
    try:
        result = run_layer2b_post_bootstrap(
            config,
            remnawave_adapter=DiscoveredRemnaWaveAdapter(state.discovered),
            bedolaga_adapter=DatabaseBedolagaAdapter(resync_subscriptions=not args.no_resync_subscriptions),
            state_store=state_store,
            progress=_status_line,
        )
    except Layer2bError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_bootstrap_real(args: argparse.Namespace, config) -> int:
    if args.adapter == 'ssh':
        return _cmd_bootstrap_ssh(args, config)
    missing = []
    if args.adapter != 'local':
        missing.append('--adapter local')
    if args.secrets_dir is None:
        missing.append('--secrets-dir')
    if args.state_dir is None:
        missing.append('--state-dir')
    if args.env_dir is None:
        missing.append('--env-dir')
    if args.render_dir is None:
        missing.append('--render-dir')
    if missing:
        print(f'bootstrap requires {", ".join(missing)} for the current safe adapter mode', file=sys.stderr)
        return 2
    try:
        result = run_layer1_local_bootstrap(
            config,
            secret_store=LocalSecretStore(args.secrets_dir),
            state_store=NodeStateStore(args.state_dir),
            output_dir=args.render_dir,
            env_store=FakeEnvironmentStore(args.env_dir),
            progress=_status_line,
        )
    except Layer1Error as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_bootstrap_ssh(args: argparse.Namespace, config) -> int:
    missing = []
    if args.secrets_dir is None:
        missing.append('--secrets-dir')
    if args.state_dir is None:
        missing.append('--state-dir')
    if args.render_dir is None:
        missing.append('--render-dir')
    if not args.root_password_ref and not args.root_private_key_ref and not args.admin_private_key_ref:
        missing.append('--root-password-ref, --root-private-key-ref or --admin-private-key-ref')
    if missing:
        print(f'bootstrap --adapter ssh requires {", ".join(missing)}', file=sys.stderr)
        return 2
    try:
        result = _run_layer1_ssh_with_admin_retry(
            config,
            secret_store=LocalSecretStore(args.secrets_dir),
            state_store=NodeStateStore(args.state_dir),
            output_dir=args.render_dir,
            args=args,
        )
    except Layer1Error as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_pre_bootstrap_real(args: argparse.Namespace, config) -> int:
    if args.adapter == 'http':
        return _cmd_pre_bootstrap_http(args, config)
    missing = []
    if args.adapter != 'local':
        missing.append('--adapter local')
    if args.env_dir is None:
        missing.append('--env-dir')
    if args.secrets_dir is None:
        missing.append('--secrets-dir')
    if args.state_dir is None:
        missing.append('--state-dir')
    if missing:
        print(f'pre-bootstrap requires {", ".join(missing)} for the current safe adapter mode', file=sys.stderr)
        return 2
    try:
        result = run_layer2a_pre_bootstrap(
            config,
            adapter=LocalRemnaWaveAdapter(
                FakeEnvironmentStore(args.env_dir),
                secret_store=LocalSecretStore(args.secrets_dir),
            ),
            secret_store=LocalSecretStore(args.secrets_dir),
            state_store=NodeStateStore(args.state_dir),
            progress=_status_line,
        )
    except Layer2aError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _cmd_pre_bootstrap_http(args: argparse.Namespace, config) -> int:
    missing = []
    if args.secrets_dir is None:
        missing.append('--secrets-dir')
    if args.state_dir is None:
        missing.append('--state-dir')
    if not args.api_key_ref:
        missing.append('--api-key-ref')
    if missing:
        print(f'pre-bootstrap --adapter http requires {", ".join(missing)}', file=sys.stderr)
        return 2
    try:
        secret_store = LocalSecretStore(args.secrets_dir)
        if not args.no_auto_warp_register:
            ensure_warp_registration_for_config(
                config,
                secret_store=secret_store,
                options=_warp_registration_options(args, secret_store),
                overwrite=False,
            )
        result = run_layer2a_pre_bootstrap(
            config,
            adapter=_http_remnawave_adapter(args, config, secret_store),
            secret_store=secret_store,
            state_store=NodeStateStore(args.state_dir),
            progress=_status_line,
        )
    except (Layer2aError, WarpRegistrationError, SecretStoreError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print('\n'.join(result.to_lines()))
    return 0


def _warp_registration_options(args: argparse.Namespace, secret_store: LocalSecretStore) -> WarpRegistrationOptions:
    license_key = secret_store.read_text(args.warp_license_key_ref) if getattr(args, 'warp_license_key_ref', None) else None
    return WarpRegistrationOptions(
        api_base_url=args.warp_api_base_url,
        api_version=args.warp_api_version,
        client_version=args.warp_client_version,
        user_agent=args.warp_user_agent,
        device_model=args.warp_device_model,
        license_key=license_key,
        timeout_seconds=args.timeout_seconds,
        verify_tls=not args.no_verify_tls,
    )


def _operator_context(args: argparse.Namespace) -> OperatorContext:
    secret_store = LocalSecretStore(args.secrets_dir)
    return OperatorContext(
        adapter=args.adapter,
        secrets_dir=args.secrets_dir,
        state_dir=args.state_dir,
        render_dir=args.render_dir,
        env_dir=args.env_dir,
        api_key_ref=args.api_key_ref,
        auth_type=args.auth_type,
        caddy_token_ref=args.caddy_token_ref,
        timeout_seconds=args.timeout_seconds,
        verify_tls=not args.no_verify_tls,
        cloudflare_api_token_ref=args.cloudflare_api_token_ref,
        skip_dns=args.skip_dns,
        no_auto_warp_register=args.no_auto_warp_register,
        warp_options=_warp_registration_options(args, secret_store),
        admin_public_key_ref=args.admin_public_key_ref,
        admin_private_key_ref=args.admin_private_key_ref,
        root_private_key_ref=args.root_private_key_ref,
        allow_admin_ssh_bootstrap=True,
        acme_email=args.acme_email,
        issue_certificates=not args.no_cert_issue,
        harden_ssh=not args.no_ssh_hardening,
        start_services=not args.no_start_services,
        resync_subscriptions=not args.no_resync_subscriptions,
        progress=_status_line,
    )


def _decommission_remnawave_adapter(args: argparse.Namespace, config, secret_store: LocalSecretStore | None):
    if args.adapter == 'none':
        return None
    if args.adapter == 'local':
        if args.env_dir is None:
            raise ValueError('delete --adapter local requires --env-dir')
        return LocalRemnaWaveAdapter(FakeEnvironmentStore(args.env_dir), secret_store=secret_store)
    if args.adapter == 'http':
        missing = []
        if secret_store is None:
            missing.append('--secrets-dir')
        if not args.api_key_ref:
            missing.append('--api-key-ref')
        if missing:
            raise ValueError(f'delete --adapter http requires {", ".join(missing)}')
        return _http_remnawave_adapter(args, config, secret_store)
    raise ValueError(f'unsupported decommission adapter: {args.adapter}')


def _decommission_bedolaga_adapter(args: argparse.Namespace):
    adapter = args.bedolaga_adapter
    if adapter == 'auto':
        adapter = {'none': 'none', 'local': 'local', 'http': 'none'}[args.adapter]
    if adapter == 'none':
        return None
    if adapter == 'local':
        if args.env_dir is None:
            raise ValueError('delete --bedolaga-adapter local requires --env-dir')
        return LocalBedolagaAdapter(FakeEnvironmentStore(args.env_dir))
    if adapter == 'db':
        return DatabaseBedolagaAdapter()
    if adapter == 'psql':
        return PsqlBedolagaAdapter(db_container=args.bedolaga_db_container)
    raise ValueError(f'unsupported Bedolaga decommission adapter: {adapter}')


def _apply_routing_from_args(args: argparse.Namespace, config) -> object:
    return apply_routing_overrides(
        config=config,
        routes_file=args.routes_file,
        remnawave_adapter=_routing_apply_adapter(args, config),
        state_store=NodeStateStore(args.state_dir) if args.state_dir else None,
    )


def _routing_apply_adapter(args: argparse.Namespace, config):
    adapter = args.adapter
    if adapter is None:
        raise ValueError('--apply requires --adapter local/http')
    if adapter == 'live':
        adapter = 'http'
    if adapter == 'local':
        if args.env_dir is None:
            raise ValueError('route apply --adapter local requires --env-dir')
        secret_store = LocalSecretStore(args.secrets_dir) if args.secrets_dir else None
        return LocalRemnaWaveAdapter(FakeEnvironmentStore(args.env_dir), secret_store=secret_store)
    if adapter == 'http':
        missing = []
        if args.secrets_dir is None:
            missing.append('--secrets-dir')
        if not args.api_key_ref:
            missing.append('--api-key-ref')
        if missing:
            raise ValueError(f'route apply --adapter http requires {", ".join(missing)}')
        secret_store = LocalSecretStore(args.secrets_dir)
        return _http_remnawave_adapter(args, config, secret_store)
    raise ValueError(f'unsupported route apply adapter: {adapter}')


def _http_remnawave_adapter(args: argparse.Namespace, config, secret_store: LocalSecretStore) -> HttpRemnaWaveAdapter:
    api_key = secret_store.read_text(args.api_key_ref)
    caddy_token = secret_store.read_text(args.caddy_token_ref) if args.caddy_token_ref else None
    return HttpRemnaWaveAdapter(
        api_url=str(args.api_url or config.main_server.remnawave_api_url),
        auth=RemnaWaveProbeAuth(api_key=api_key, auth_type=args.auth_type, caddy_token=caddy_token),
        secret_store=secret_store,
        timeout_seconds=args.timeout_seconds,
        verify_tls=not args.no_verify_tls,
    )


def _print_state_summary(internal_name: str, state_dir: Path) -> None:
    state = NodeStateStore(state_dir).load(internal_name)
    if state is None:
        print(f'State: missing in {state_dir}')
        return
    print(f'State: last_completed_step={state.last_completed_step or "<none>"} run_id={state.run_id}')


def _common_generation_input(args: argparse.Namespace) -> CommonGenerationInput:
    return CommonGenerationInput(
        main_ipv4=args.main_ipv4,
        remnawave_api_url=args.remnawave_api_url,
        admin_allowlist=tuple(args.admin_allowlist),
        admin_user=args.admin_user,
        ssh_port=args.ssh_port,
        dns_api_token_ref=args.dns_api_token_ref,
    )


def _node_generation_input(args: argparse.Namespace, prefix: str) -> NodeGenerationInput:
    attr_prefix = f'{prefix}_' if prefix else ''
    return NodeGenerationInput(
        internal_name=getattr(args, f'{attr_prefix}internal_name'),
        display_name=getattr(args, f'{attr_prefix}display_name'),
        country_code=getattr(args, f'{attr_prefix}country_code'),
        domain=getattr(args, f'{attr_prefix}domain'),
        public_ipv4=getattr(args, f'{attr_prefix}ipv4'),
        public_ipv6=getattr(args, f'{attr_prefix}ipv6'),
        spare_domains=tuple(getattr(args, f'{attr_prefix}spare_domains')),
    )


def _tariffs_from_args(args: argparse.Namespace, prefix: str) -> TariffTargets:
    attr_prefix = f'{prefix}_' if prefix else ''
    specific = TariffTargets(
        slugs=tuple(getattr(args, f'{attr_prefix}tariff_slugs')),
        names=tuple(getattr(args, f'{attr_prefix}tariff_names')),
        trial_eligible=bool(getattr(args, f'{attr_prefix}trial_eligible')),
    )
    if prefix and specific.has_any():
        return specific
    return TariffTargets(
        slugs=tuple(args.tariff_slugs),
        names=tuple(args.tariff_names),
        trial_eligible=bool(args.trial_eligible),
    )


def _print_written_configs(paths: list[Path]) -> None:
    print(f'Generated configs: {len(paths)}')
    for path in paths:
        print(path)


def _quick_paths(args: argparse.Namespace) -> argparse.Namespace:
    work_root = Path(args.work_root).expanduser().resolve()
    return argparse.Namespace(
        work_root=work_root,
        configs_dir=Path(args.configs_dir or work_root / 'configs').expanduser().resolve(),
        secrets_dir=Path(args.secrets_dir or DEFAULT_NODE_SECRETS_DIR).expanduser().resolve(),
        state_dir=Path(args.state_dir or work_root / 'state').expanduser().resolve(),
        render_dir=Path(args.render_dir or work_root / 'render').expanduser().resolve(),
        routes_file=Path(args.routes_file or work_root / 'routes.yml').expanduser().resolve(),
        env_dir=Path(args.env_dir or work_root / 'env').expanduser().resolve(),
    )


def _quick_common_generation_input(args: argparse.Namespace) -> CommonGenerationInput:
    return CommonGenerationInput(
        main_ipv4=args.main_ipv4,
        remnawave_api_url=args.remnawave_api_url.rstrip('/'),
        admin_allowlist=tuple(args.admin_allowlist or DEFAULT_QUICK_ADMIN_ALLOWLIST),
        admin_user=args.admin_user,
        ssh_port=args.ssh_port,
        dns_api_token_ref=args.dns_api_token_ref,
    )


def _prompt_quick_node_generation_input(
    label: str,
    *,
    country_default: str | None,
    require_domain: bool,
) -> NodeGenerationInput:
    print(f'\n{label}')
    display_name = _prompt('Name in app')
    internal_name = _prompt('Internal node name', default=_slug(display_name))
    country_code = _prompt('Country code', default=country_default) if country_default else _prompt('Country code')
    domain = _prompt('Node domain/subdomain') if require_domain else _prompt_optional('Node domain/subdomain')
    public_ipv4 = _prompt('Public IPv4')
    public_ipv6 = _prompt_optional('Public IPv6')
    spare_domains = tuple(_prompt_csv('Spare domains', required=False)) if domain else ()
    return NodeGenerationInput(
        internal_name=internal_name,
        display_name=display_name,
        country_code=country_code,
        domain=domain,
        public_ipv4=public_ipv4,
        public_ipv6=public_ipv6,
        spare_domains=spare_domains,
    )


def _prompt_quick_tariffs(label: str, *, default_preset: str) -> TariffTargets:
    print(f'\n{label}')
    print('Tariff presets:')
    for key, title, _names, _trial_eligible in DEFAULT_QUICK_TARIFF_PRESETS:
        print(f'  {key}. {title}')
    raw = _prompt(
        'Choose presets or type tariff names (comma-separated)',
        default=default_preset,
    )
    names: list[str] = []
    trial_eligible = False
    preset_by_key = {key: (preset_names, is_trial) for key, _title, preset_names, is_trial in DEFAULT_QUICK_TARIFF_PRESETS}
    preset_by_title = {
        title.lower(): (preset_names, is_trial)
        for _key, title, preset_names, is_trial in DEFAULT_QUICK_TARIFF_PRESETS
    }
    for item in [part.strip() for part in raw.split(',') if part.strip()]:
        normalized = item.lower()
        preset = preset_by_key.get(item) or preset_by_title.get(normalized)
        if preset:
            preset_names, is_trial = preset
            names.extend(preset_names)
            trial_eligible = trial_eligible or is_trial
        elif normalized in {'trial', 'триал'}:
            trial_eligible = True
        else:
            names.append(item)
    slugs = tuple(_prompt_csv('Extra tariff slugs', required=False))
    unique_names = tuple(dict.fromkeys(names))
    return TariffTargets(slugs=slugs, names=unique_names, trial_eligible=trial_eligible)


def _quick_root_ssh_auth_refs(
    args: argparse.Namespace,
    paths: argparse.Namespace,
    secret_store: LocalSecretStore,
    label: str,
    internal_name: str,
    *,
    live: bool,
    default_password_ref: str | None = None,
) -> tuple[str | None, str | None]:
    password_ref = default_password_ref or f'secrets/ssh-root-password-{_slug(internal_name)}'
    if not live:
        return password_ref, None

    password_exists = secret_store.path_for_ref(password_ref).is_file()
    default_key_ref = args.root_private_key_ref or args.admin_private_key_ref
    key_exists = bool(default_key_ref and _secret_ref_exists(paths.secrets_dir, default_key_ref))

    prompt = f'{label} root password'
    if password_exists and key_exists:
        prompt += f' [empty = reuse existing secret, type key = use SSH key {default_key_ref}]'
    elif password_exists:
        prompt += ' [empty = reuse existing secret]'
    elif key_exists:
        prompt += f' [empty = use SSH key {default_key_ref}]'
    else:
        prompt += ' [empty = key-only SSH]'

    value = getpass.getpass(prompt + ': ').strip()
    if value.lower() in {'key', 'ssh-key', 'ssh'} and key_exists:
        print(f'Root SSH private key will be used: {default_key_ref}')
        return None, default_key_ref
    if value:
        secret_store.write_text(password_ref, value, overwrite=True)
        print(f'Root password secret written: {password_ref}')
        return password_ref, None
    if password_exists:
        print(f'Root password secret reused: {password_ref}')
        return password_ref, None
    if key_exists:
        print(f'Root SSH private key will be used: {default_key_ref}')
        return None, default_key_ref

    key_path_raw = input(f'{label} root SSH private key path [empty = cancel]: ').strip()
    if key_path_raw:
        key_path = Path(key_path_raw).expanduser()
        if not key_path.is_file():
            raise ValueError(f'{label} root SSH private key file not found: {key_path}')
        key_ref = args.root_private_key_ref or f'secrets/ssh-root-private-key-{_slug(internal_name)}'
        secret_store.write_text(key_ref, key_path.read_text(encoding='utf-8'), overwrite=True)
        print(f'Root SSH private key secret written: {key_ref}')
        return None, key_ref

    raise ValueError(f'{label} root password or root SSH private key is required; no existing secret at {password_ref}')


def _quick_root_password_ref(
    secret_store: LocalSecretStore,
    label: str,
    internal_name: str,
    *,
    live: bool,
    default_ref: str | None = None,
) -> str | None:
    ref = default_ref or f'secrets/ssh-root-password-{_slug(internal_name)}'
    if not live:
        return ref
    exists = secret_store.path_for_ref(ref).is_file()
    prompt = f'{label} root password'
    if exists:
        prompt += ' [empty = reuse existing secret]'
    value = getpass.getpass(prompt + ': ').strip()
    if value:
        secret_store.write_text(ref, value, overwrite=True)
        print(f'Root password secret written: {ref}')
        return ref
    if exists:
        print(f'Root password secret reused: {ref}')
        return ref
    raise ValueError(f'{label} root password is required; no existing secret at {ref}')


def _quick_existing_node_root_ssh_auth_refs(
    args: argparse.Namespace,
    paths: argparse.Namespace,
    secret_store: LocalSecretStore,
    label: str,
    config,
    *,
    live: bool,
) -> tuple[str | None, str | None]:
    if not live:
        return None, None
    if args.admin_private_key_ref and _secret_ref_exists(paths.secrets_dir, args.admin_private_key_ref):
        print(f'{label} refresh will use admin SSH key: {args.admin_private_key_ref}')
        return None, None
    return _quick_root_ssh_auth_refs(
        args,
        paths,
        secret_store,
        label,
        config.display.internal_name,
        live=True,
        default_password_ref=_default_root_password_ref(paths.secrets_dir, config),
    )


def _quick_existing_node_root_password_ref(
    args: argparse.Namespace,
    paths: argparse.Namespace,
    secret_store: LocalSecretStore,
    label: str,
    config,
    *,
    live: bool,
) -> str | None:
    password_ref, _key_ref = _quick_existing_node_root_ssh_auth_refs(
        args,
        paths,
        secret_store,
        label,
        config,
        live=live,
    )
    return password_ref


def _quick_operator_context(
    args: argparse.Namespace,
    paths: argparse.Namespace,
    *,
    allow_admin_ssh_bootstrap: bool = False,
) -> OperatorContext:
    secret_store = LocalSecretStore(paths.secrets_dir)
    auth_type, caddy_token_ref = _quick_resolved_auth(args, paths.secrets_dir)
    return OperatorContext(
        adapter=args.adapter,
        secrets_dir=paths.secrets_dir,
        state_dir=paths.state_dir,
        render_dir=paths.render_dir,
        env_dir=paths.env_dir,
        api_key_ref=args.api_key_ref,
        auth_type=auth_type,
        caddy_token_ref=caddy_token_ref,
        timeout_seconds=args.timeout_seconds,
        verify_tls=not args.no_verify_tls,
        cloudflare_api_token_ref=args.cloudflare_api_token_ref,
        skip_dns=args.skip_dns,
        no_auto_warp_register=args.no_auto_warp_register,
        warp_options=_warp_registration_options(args, secret_store),
        admin_public_key_ref=args.admin_public_key_ref,
        admin_private_key_ref=args.admin_private_key_ref,
        root_private_key_ref=args.root_private_key_ref,
        allow_admin_ssh_bootstrap=allow_admin_ssh_bootstrap,
        acme_email=args.acme_email,
        issue_certificates=not args.no_cert_issue,
        harden_ssh=not args.no_ssh_hardening,
        start_services=not args.no_start_services,
        resync_subscriptions=not args.no_resync_subscriptions,
        progress=_status_line,
    )


def _quick_routing_adapter(args: argparse.Namespace, paths: argparse.Namespace, config):
    if args.adapter == 'local':
        return LocalRemnaWaveAdapter(FakeEnvironmentStore(paths.env_dir), secret_store=LocalSecretStore(paths.secrets_dir))
    auth_type, caddy_token_ref = _quick_resolved_auth(args, paths.secrets_dir)
    adapter_args = argparse.Namespace(
        api_url=None,
        api_key_ref=args.api_key_ref,
        auth_type=auth_type,
        caddy_token_ref=caddy_token_ref,
        timeout_seconds=args.timeout_seconds,
        no_verify_tls=args.no_verify_tls,
    )
    return _http_remnawave_adapter(adapter_args, config, LocalSecretStore(paths.secrets_dir))


def _quick_resolved_auth(args: argparse.Namespace, secrets_dir: Path) -> tuple[str, str | None]:
    if args.auth_type != 'auto':
        return args.auth_type, args.caddy_token_ref
    if args.caddy_token_ref and _secret_ref_exists(secrets_dir, args.caddy_token_ref):
        return 'caddy', args.caddy_token_ref
    return 'api_key', None


def _quick_select_config_path(
    paths: argparse.Namespace,
    extra_dirs: list[Path],
    *,
    label: str,
    roles: tuple[str, ...],
) -> Path:
    records = _discover_decommission_configs([paths.configs_dir, *extra_dirs])
    if roles:
        role_values = set(roles)
        records = [record for record in records if record['config'].role.value in role_values]
    if not records:
        role_text = ', '.join(roles) if roles else 'node'
        raise ValueError(f'No {role_text} configs found in {paths.configs_dir}')
    print(f'\n{label}')
    _print_decommission_candidates(records)
    while True:
        answer = input(f'Select server [1-{len(records)}]: ').strip()
        try:
            index = int(answer)
        except ValueError:
            print('Enter a number from the list.', file=sys.stderr)
            continue
        if 1 <= index <= len(records):
            return Path(records[index - 1]['path'])
        print('Selected number is out of range.', file=sys.stderr)


def _backup_existing_config(path: Path, *, label: str = 'Foreign config backup') -> Path | None:
    config_path = Path(path)
    if not config_path.exists():
        return None
    backup_path = config_path.with_suffix(config_path.suffix + '.bak')
    index = 1
    while backup_path.exists():
        backup_path = config_path.with_suffix(config_path.suffix + f'.bak{index}')
        index += 1
    backup_path.write_text(config_path.read_text(encoding='utf-8'), encoding='utf-8')
    print(f'{label}: {backup_path}')
    return backup_path


def _print_quick_paths(paths: argparse.Namespace) -> None:
    print('Quick paths:')
    print(f'  configs: {paths.configs_dir}')
    print(f'  secrets: {paths.secrets_dir}')
    print(f'  state: {paths.state_dir}')
    print(f'  render: {paths.render_dir}')
    print(f'  routes: {paths.routes_file}')


def _read_secret_input(args: argparse.Namespace) -> str:
    input_modes = [bool(args.stdin), args.from_file is not None]
    if sum(input_modes) > 1:
        raise ValueError('choose only one of --stdin or --from-file')
    if args.stdin:
        return sys.stdin.read().strip()
    if args.from_file is not None:
        return args.from_file.read_text(encoding='utf-8').strip()
    return getpass.getpass('Secret value: ').strip()


def _resolve_remnawave_api_url(args: argparse.Namespace) -> str:
    if args.api_url:
        return args.api_url
    if args.config is None:
        raise ValueError('remnawave-check requires either config or --api-url')
    config = load_node_config(args.config)
    return str(config.main_server.remnawave_api_url).rstrip('/')


def _prompt_common_generation_input() -> CommonGenerationInput:
    return CommonGenerationInput(
        main_ipv4=_prompt('Main/control-plane IPv4'),
        remnawave_api_url=_prompt('RemnaWave API URL', default='https://panel.example.com'),
        admin_allowlist=tuple(_prompt_csv('Admin allowlist IPv4 list')),
        admin_user=_prompt('Admin SSH user to create', default='templar'),
        ssh_port=int(_prompt('SSH port', default='22')),
        dns_api_token_ref=_prompt('DNS API token secret ref', default='secrets/dns-api-token'),
    )


def _prompt_node_generation_input(
    label: str,
    *,
    country_default: str | None,
    require_domain: bool = True,
) -> NodeGenerationInput:
    print(f'\n{label}')
    country = _prompt('Country code', default=country_default) if country_default else _prompt('Country code')
    return NodeGenerationInput(
        internal_name=_prompt('Internal node name'),
        display_name=_prompt('User-facing connection name'),
        country_code=country,
        domain=_prompt('Node domain') if require_domain else _prompt_optional('Node domain'),
        public_ipv4=_prompt('Public IPv4'),
        public_ipv6=_prompt_optional('Public IPv6'),
        spare_domains=tuple(_prompt_csv('Spare domains', required=False)),
    )


def _prompt_tariffs(label: str) -> TariffTargets:
    print(f'\n{label}')
    slugs = tuple(_prompt_csv('Tariff slugs', required=False))
    names = tuple(_prompt_csv('Tariff names', required=False))
    trial_eligible = _prompt_choice('Include free trial pool (yes/no)', choices=('yes', 'no'), default='no') == 'yes'
    return TariffTargets(slugs=slugs, names=names, trial_eligible=trial_eligible)


def _default_server_name_for_target(target: str) -> str:
    host, separator, _port = target.strip().partition(':')
    if separator == ':' and host:
        return host.lower()
    return DEFAULT_REMOTE_DEST_SERVER_NAME


def _prompt(label: str, *, default: str | None = None) -> str:
    suffix = f' [{default}]' if default is not None else ''
    while True:
        value = input(f'{label}{suffix}: ').strip()
        if value:
            return value
        if default is not None:
            return default
        print('Value is required.')


def _prompt_optional(label: str) -> str | None:
    value = input(f'{label} [empty]: ').strip()
    return value or None


def _prompt_csv(label: str, *, required: bool = True, default: str | None = None) -> list[str]:
    while True:
        suffix = f' [{default}]' if default is not None else ''
        raw = input(f'{label} (comma-separated){"" if required else " [empty]"}{suffix}: ').strip()
        if not raw and default is not None:
            raw = default
        values = [item.strip() for item in raw.split(',') if item.strip()]
        if values or not required:
            return values
        print('At least one value is required.')


def _prompt_choice(label: str, *, choices: tuple[str, ...], default: str) -> str:
    choices_text = '/'.join(choices)
    while True:
        value = _prompt(f'{label} ({choices_text})', default=default)
        if value in choices:
            return value
        print(f'Choose one of: {choices_text}.')


if __name__ == '__main__':
    raise SystemExit(main())
