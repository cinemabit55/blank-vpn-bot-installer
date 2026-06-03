"""Render full Xray config profiles for RemnaWave."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.templar_node.credentials import (
    CredentialError,
    RealityCredentials,
    WarpRegistration,
    ensure_reality_credentials,
    ensure_vless_uuid,
    read_reality_credentials,
    read_warp_registration,
)
from app.templar_node.routes import RouteOverrides
from app.templar_node.schemas import DefaultRoute, NodeConfig, NodeRole, RealityStrategy, RealityTransport, TransitMode, WarpMode
from app.templar_node.secrets import LocalSecretStore, SecretStoreError


PUBLIC_INBOUND_TAG = 'VLESS_Public'
DIRECT_TAG = 'DIRECT'
BLOCK_TAG = 'BLOCK'
REALITY_CLIENT_FINGERPRINT = 'firefox'
DISCORD_DIRECT_DOMAINS = (
    'geosite:discord',
    'domain:discord.com',
    'domain:discord.gg',
    'domain:discord.media',
    'domain:discordapp.com',
    'domain:discordapp.net',
    'domain:discordcdn.com',
)
DISCORD_DIRECT_IPS = ('66.22.192.0/18',)
DISCORD_VOICE_UDP_PORTS = '50000-65535'
TELEGRAM_DIRECT_IPS = ('geoip:telegram',)
ADBLOCK_DOMAINS = (
    'geosite:category-ads-all',
    'domain:doubleclick.net',
    'domain:googleadservices.com',
    'domain:googlesyndication.com',
    'domain:googletagservices.com',
    'domain:google-analytics.com',
    'domain:adservice.google.com',
    'domain:ads.youtube.com',
    'domain:pagead2.googlesyndication.com',
    'domain:pubads.g.doubleclick.net',
    'domain:imasdk.googleapis.com',
    'domain:ads.tiktok.com',
    'domain:business-api.tiktok.com',
    'domain:ads-api.tiktok.com',
    'domain:ads-twitter.com',
    'domain:analytics.twitter.com',
    'domain:adsrvr.org',
    'domain:criteo.com',
    'domain:criteo.net',
    'domain:scorecardresearch.com',
)
RU_DIRECT_DOMAINS = (
    'geosite:category-ru',
    'domain:ru',
    'domain:рф',
    'domain:vk.com',
    'domain:vk.ru',
    'domain:vkvideo.ru',
    'domain:vkuser.net',
    'domain:vkuseraudio.net',
    'domain:vkuserlive.net',
    'domain:ok.ru',
    'domain:mail.ru',
    'domain:mycdn.me',
    'domain:gosuslugi.ru',
    'domain:mos.ru',
    'domain:mosreg.ru',
    'domain:nalog.gov.ru',
    'domain:sberbank.ru',
    'domain:sber.ru',
    'domain:tbank.ru',
    'domain:tinkoff.ru',
    'domain:vtb.ru',
    'domain:alfabank.ru',
    'domain:gazprombank.ru',
    'domain:mironline.ru',
    'domain:yandex.ru',
    'domain:yandex.net',
    'domain:ya.ru',
    'domain:ozon.ru',
    'domain:wildberries.ru',
    'domain:whoosh.bike',
    'domain:whoosh-bike.ru',
    'domain:data.whoosh-bike.ru',
)
RU_DIRECT_IPS = ('geoip:ru',)


def public_inbound_tag(config: NodeConfig) -> str:
    """Return a globally unique public inbound tag for a node profile."""
    return f'{PUBLIC_INBOUND_TAG}_{_tag_suffix(config.display.internal_name)}'


def _tag_suffix(value: str) -> str:
    suffix = ''.join(ch.upper() if ch.isalnum() else '_' for ch in value).strip('_')
    while '__' in suffix:
        suffix = suffix.replace('__', '_')
    return suffix or 'NODE'


class XrayProfileRenderError(ValueError):
    """Raised when a full RemnaWave/Xray profile cannot be rendered safely."""


@dataclass(frozen=True)
class RenderedXrayProfile:
    name: str
    config: dict[str, Any]
    public_inbound_tag: str = PUBLIC_INBOUND_TAG

    def inbound_tags(self) -> tuple[str, ...]:
        return tuple(item.get('tag') for item in self.config.get('inbounds', []) if isinstance(item.get('tag'), str))


def render_xray_config_profile(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    ensure_missing: bool = True,
    route_overrides: RouteOverrides | None = None,
) -> RenderedXrayProfile:
    """Render one complete Xray config profile for a RemnaWave node.

    The function is intentionally strict: missing WARP registrations or
    malformed transit credentials stop the run before any RemnaWave write.
    """
    public_tag = public_inbound_tag(config)
    profile = {
        'log': {'loglevel': 'info'},
        'inbounds': _render_inbounds(config, secret_store=secret_store, ensure_missing=ensure_missing, public_tag=public_tag),
        'outbounds': _render_outbounds(config, secret_store=secret_store),
        'routing': _render_routing(config, route_overrides=route_overrides, public_tag=public_tag),
    }
    dns = _render_dns(config)
    if dns:
        profile['dns'] = dns
    return RenderedXrayProfile(name=_profile_name(config), config=profile, public_inbound_tag=public_tag)


def _render_inbounds(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    ensure_missing: bool,
    public_tag: str,
) -> list[dict[str, Any]]:
    inbounds = [_public_vless_inbound(config, secret_store=secret_store, ensure_missing=ensure_missing, tag=public_tag)]
    if config.role == NodeRole.FOREIGN_EXIT and config.transit.mode == TransitMode.VLESS_REALITY:
        inbounds.append(_transit_vless_inbound(config, secret_store=secret_store, ensure_missing=ensure_missing))
    return inbounds


def _public_vless_inbound(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    ensure_missing: bool,
    tag: str,
) -> dict[str, Any]:
    credentials = _public_reality_credentials(config, secret_store=secret_store, ensure_missing=ensure_missing)
    return {
        'tag': tag,
        'listen': '0.0.0.0',
        'port': config.reality.public_port,
        'protocol': 'vless',
        'settings': {'clients': [], 'decryption': 'none'},
        'streamSettings': _public_stream_settings(config, credentials),
        'sniffing': _sniffing(),
    }


def _transit_vless_inbound(config: NodeConfig, *, secret_store: LocalSecretStore, ensure_missing: bool) -> dict[str, Any]:
    credentials = _transit_reality_credentials(config, secret_store=secret_store, ensure_missing=ensure_missing)
    return {
        'tag': config.transit.inbound_tag,
        'listen': '0.0.0.0',
        'port': config.transit.listen_port,
        'protocol': 'vless',
        'settings': {'clients': _transit_service_clients(config, secret_store=secret_store, ensure_missing=ensure_missing), 'decryption': 'none'},
        'streamSettings': {
            'network': 'tcp',
            'security': 'reality',
            'realitySettings': {
                'show': False,
                'dest': _inbound_reality_dest(config),
                'xver': 0,
                'serverNames': list(credentials.server_names),
                'privateKey': credentials.private_key,
                'shortIds': list(credentials.short_ids),
            },
        },
        'sniffing': _sniffing(),
    }


def _transit_service_clients(config: NodeConfig, *, secret_store: LocalSecretStore, ensure_missing: bool) -> list[dict[str, Any]]:
    if not config.transit.service_user_credential_ref:
        return []
    try:
        user_uuid = (
            ensure_vless_uuid(secret_store, config.transit.service_user_credential_ref)
            if ensure_missing
            else secret_store.read_text(config.transit.service_user_credential_ref).strip()
        )
    except (CredentialError, SecretStoreError) as exc:
        raise XrayProfileRenderError(str(exc)) from exc
    if not user_uuid:
        raise XrayProfileRenderError(f'transit service user secret {config.transit.service_user_credential_ref} is empty')
    return [{'id': user_uuid, 'flow': config.transit.flow}]


def _render_outbounds(config: NodeConfig, *, secret_store: LocalSecretStore) -> list[dict[str, Any]]:
    outbounds: list[dict[str, Any]] = [
        {'tag': DIRECT_TAG, 'protocol': 'freedom'},
        {'tag': BLOCK_TAG, 'protocol': 'blackhole'},
    ]
    if config.warp.mode == WarpMode.XRAY_NATIVE:
        outbounds.append(_warp_outbound(config, secret_store=secret_store))
    if config.role == NodeRole.RU_EDGE and config.transit.mode == TransitMode.VLESS_REALITY:
        outbounds.append(_transit_outbound(config, secret_store=secret_store))
        for backup in config.transit.backup_outbounds:
            outbounds.append(
                _transit_outbound_from_parts(
                    tag=backup.tag,
                    address=backup.domain,
                    port=backup.port,
                    server_names=backup.server_names,
                    service_user_credential_ref=backup.service_user_credential_ref,
                    reality_credentials_ref=backup.reality_credentials_ref,
                    secret_store=secret_store,
                ),
            )
    return outbounds


def _warp_outbound(config: NodeConfig, *, secret_store: LocalSecretStore) -> dict[str, Any]:
    if not config.warp.registration_ref:
        raise XrayProfileRenderError('xray_native WARP requires warp.registration_ref')
    try:
        registration = read_warp_registration(secret_store, config.warp.registration_ref)
    except CredentialError as exc:
        raise XrayProfileRenderError(str(exc)) from exc
    return _warp_outbound_from_registration(config.warp.outbound_tag or 'WARP_OUT', registration)


def _warp_outbound_from_registration(tag: str, registration: WarpRegistration) -> dict[str, Any]:
    return {
        'tag': tag,
        'protocol': 'wireguard',
        'settings': {
            'secretKey': registration.secret_key,
            'address': list(registration.address),
            'peers': [
                {
                    'publicKey': registration.peer_public_key,
                    'endpoint': registration.endpoint,
                    'keepAlive': registration.keep_alive,
                },
            ],
            'reserved': list(registration.reserved),
            'mtu': registration.mtu,
            'kernelMode': False,
            'domainStrategy': 'ForceIPv4',
        },
    }


def _transit_outbound(config: NodeConfig, *, secret_store: LocalSecretStore) -> dict[str, Any]:
    if not config.transit.foreign_exit_domain or not config.transit.foreign_exit_port:
        raise XrayProfileRenderError('transit outbound requires foreign_exit_domain and foreign_exit_port')
    return _transit_outbound_from_parts(
        tag=config.transit.outbound_tag or 'TRANSIT_TO_FOREIGN',
        address=config.transit.foreign_exit_domain,
        port=config.transit.foreign_exit_port,
        server_names=config.transit.server_names or [config.transit.foreign_exit_domain],
        service_user_credential_ref=config.transit.service_user_credential_ref,
        reality_credentials_ref=config.transit.reality_credentials_ref,
        secret_store=secret_store,
    )


def _transit_outbound_from_parts(
    *,
    tag: str,
    address: str,
    port: int,
    server_names: list[str],
    service_user_credential_ref: str | None,
    reality_credentials_ref: str | None,
    secret_store: LocalSecretStore,
) -> dict[str, Any]:
    if not service_user_credential_ref:
        raise XrayProfileRenderError('transit outbound requires service_user_credential_ref')
    if not reality_credentials_ref:
        raise XrayProfileRenderError('transit outbound requires reality_credentials_ref')
    try:
        user_uuid = secret_store.read_text(service_user_credential_ref)
    except SecretStoreError as exc:
        raise XrayProfileRenderError(str(exc)) from exc
    credentials = read_reality_credentials(secret_store, reality_credentials_ref)
    if tuple(server_names) != credentials.server_names:
        raise XrayProfileRenderError('transit.server_names does not match reality_credentials_ref serverNames')
    return {
        'tag': tag,
        'protocol': 'vless',
        'settings': {
            'vnext': [
                {
                    'address': address,
                    'port': port,
                    'users': [
                        {
                            'id': user_uuid,
                            'encryption': 'none',
                            'flow': 'xtls-rprx-vision',
                        },
                    ],
                },
            ],
        },
        'streamSettings': {
            'network': 'tcp',
            'security': 'reality',
            'realitySettings': {
                'serverName': credentials.server_names[0],
                'fingerprint': REALITY_CLIENT_FINGERPRINT,
                'publicKey': credentials.public_key,
                'shortId': credentials.short_ids[0],
                'spiderX': '/',
            },
        },
    }


def _render_routing(config: NodeConfig, *, route_overrides: RouteOverrides | None = None, public_tag: str) -> dict[str, Any]:
    rules: list[dict[str, Any]] = [
        {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': BLOCK_TAG},
        {'type': 'field', 'domain': ['geosite:private'], 'outboundTag': BLOCK_TAG},
        {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': BLOCK_TAG},
        {'type': 'field', 'domain': list(ADBLOCK_DOMAINS), 'outboundTag': BLOCK_TAG},
    ]
    if config.role == NodeRole.RU_EDGE and config.routing is not None:
        rules.extend(_ru_edge_dns_rules(config))
        rules.extend(_ru_edge_direct_rules(inbound_tags=[public_tag]))
        override_rule = _route_override_rule(route_overrides)
        if override_rule is not None:
            rules.append(override_rule)
        default_tag = config.transit.outbound_tag or 'TRANSIT_TO_FOREIGN'
        if config.routing.default_route == DefaultRoute.BLOCK:
            default_tag = BLOCK_TAG
        elif config.routing.default_route == DefaultRoute.DIRECT:
            default_tag = DIRECT_TAG
        rules.append({'type': 'field', 'inboundTag': [public_tag], 'outboundTag': default_tag})
    elif config.warp.mode == WarpMode.XRAY_NATIVE:
        inbound_tags = _warp_inbound_tags(config, public_tag)
        warp_tag = config.warp.outbound_tag or 'WARP_OUT'
        if config.warp.discord_direct:
            rules.extend(_discord_direct_rules(inbound_tags=inbound_tags))
        rules.extend(_telegram_direct_rules(inbound_tags=inbound_tags))
        rules.append({'type': 'field', 'inboundTag': inbound_tags, 'outboundTag': warp_tag})
    return {'domainStrategy': 'IPIfNonMatch', 'rules': rules}


def _warp_inbound_tags(config: NodeConfig, public_tag: str) -> list[str]:
    inbound_tags = [public_tag]
    if config.role == NodeRole.FOREIGN_EXIT and config.transit.inbound_tag:
        inbound_tags.append(config.transit.inbound_tag)
    return inbound_tags


def _route_override_rule(route_overrides: RouteOverrides | None) -> dict[str, Any] | None:
    if route_overrides is None or route_overrides.empty:
        return None
    domains = _dedupe(_xray_domain_match(domain) for domain in route_overrides.domains)
    ips = _dedupe(route_overrides.ips)
    if not domains and not ips:
        return None
    rule: dict[str, Any] = {'type': 'field', 'outboundTag': DIRECT_TAG}
    if domains:
        rule['domain'] = domains
    if ips:
        rule['ip'] = ips
    return rule


def _discord_direct_rules(*, inbound_tags: list[str]) -> list[dict[str, Any]]:
    return [
        {
            'type': 'field',
            'inboundTag': inbound_tags,
            'domain': list(DISCORD_DIRECT_DOMAINS),
            'outboundTag': DIRECT_TAG,
        },
        {
            'type': 'field',
            'inboundTag': inbound_tags,
            'ip': list(DISCORD_DIRECT_IPS),
            'outboundTag': DIRECT_TAG,
        },
        {
            'type': 'field',
            'inboundTag': inbound_tags,
            'network': 'udp',
            'port': DISCORD_VOICE_UDP_PORTS,
            'outboundTag': DIRECT_TAG,
        },
    ]


def _telegram_direct_rules(*, inbound_tags: list[str]) -> list[dict[str, Any]]:
    return [
        {
            'type': 'field',
            'inboundTag': inbound_tags,
            'network': 'udp',
            'ip': list(TELEGRAM_DIRECT_IPS),
            'outboundTag': DIRECT_TAG,
        },
    ]


def _ru_edge_direct_rules(*, inbound_tags: list[str]) -> list[dict[str, Any]]:
    return [
        {
            'type': 'field',
            'inboundTag': inbound_tags,
            'ip': list(RU_DIRECT_IPS),
            'outboundTag': DIRECT_TAG,
        },
        {
            'type': 'field',
            'inboundTag': inbound_tags,
            'domain': list(RU_DIRECT_DOMAINS),
            'outboundTag': DIRECT_TAG,
        },
    ]


def _xray_domain_match(value: str) -> str:
    normalized = value.strip().lower().rstrip('.').removeprefix('*.')
    return f'domain:{normalized}'


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _ru_edge_dns_rules(config: NodeConfig) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    if config.routing and config.routing.ru_dns:
        rules.extend(
            [
                {'type': 'field', 'domain': ['full:common.dot.dns.yandex.net'], 'outboundTag': DIRECT_TAG},
                {'type': 'field', 'ip': ['77.88.8.8', '77.88.8.1'], 'outboundTag': DIRECT_TAG},
            ],
        )
    if config.routing and config.routing.foreign_dns_via_transit:
        rules.extend(
            [
                {
                    'type': 'field',
                    'domain': ['full:cloudflare-dns.com'],
                    'outboundTag': config.transit.outbound_tag or 'TRANSIT_TO_FOREIGN',
                },
                {'type': 'field', 'ip': ['1.1.1.1'], 'outboundTag': config.transit.outbound_tag or 'TRANSIT_TO_FOREIGN'},
            ],
        )
    return rules


def _render_dns(config: NodeConfig) -> dict[str, Any] | None:
    if config.role != NodeRole.RU_EDGE or config.routing is None:
        return None
    servers = [
        {
            'address': 'https://common.dot.dns.yandex.net/dns-query',
            'domains': ['geosite:category-ru', 'domain:ru', 'domain:рф'],
            'skipFallback': False,
        },
        {
            'address': 'tcp://77.88.8.8:53',
            'domains': ['geosite:category-ru', 'domain:ru', 'domain:рф'],
            'skipFallback': False,
        },
        {
            'address': '77.88.8.1',
            'domains': ['geosite:category-ru', 'domain:ru', 'domain:рф'],
            'skipFallback': True,
        },
        'https://cloudflare-dns.com/dns-query',
    ]
    return {
        'servers': servers,
        'queryStrategy': 'UseIPv4',
        'disableFallbackIfMatch': True,
    }


def _inbound_reality_settings(config: NodeConfig, credentials: RealityCredentials) -> dict[str, Any]:
    return {
        'show': False,
        'dest': _inbound_reality_dest(config),
        'xver': 0,
        'serverNames': list(credentials.server_names),
        'privateKey': credentials.private_key,
        'shortIds': list(credentials.short_ids),
    }


def _public_stream_settings(config: NodeConfig, credentials: RealityCredentials) -> dict[str, Any]:
    settings: dict[str, Any] = {
        'network': config.reality.transport.value,
        'security': 'reality',
        'realitySettings': _inbound_reality_settings(config, credentials),
    }
    if config.reality.transport == RealityTransport.XHTTP:
        if config.reality.xhttp is None:
            raise XrayProfileRenderError('reality.transport=xhttp requires reality.xhttp')
        xhttp_settings: dict[str, Any] = {
            'path': config.reality.xhttp.path,
            'mode': config.reality.xhttp.mode.value,
        }
        if config.reality.xhttp.host:
            xhttp_settings['host'] = config.reality.xhttp.host
        settings['xhttpSettings'] = xhttp_settings
    return settings


def _inbound_reality_dest(config: NodeConfig) -> str:
    dest = f'{config.reality.local_decoy_addr}:{config.reality.local_decoy_port}'
    if config.reality.strategy == RealityStrategy.REMOTE_DEST:
        return config.reality.target or dest
    return dest


def _public_reality_credentials(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    ensure_missing: bool,
) -> RealityCredentials:
    if not config.reality.credentials_ref:
        raise XrayProfileRenderError('reality.credentials_ref is required to render live Xray profile')
    server_names = config.effective_reality_server_names()
    try:
        credentials = (
            ensure_reality_credentials(secret_store, config.reality.credentials_ref, server_names=server_names)
            if ensure_missing
            else read_reality_credentials(secret_store, config.reality.credentials_ref)
        )
    except CredentialError as exc:
        raise XrayProfileRenderError(str(exc)) from exc
    if tuple(server_names) != credentials.server_names:
        raise XrayProfileRenderError('reality.server_names does not match reality.credentials_ref serverNames')
    return credentials


def _transit_reality_credentials(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    ensure_missing: bool,
) -> RealityCredentials:
    if not config.transit.reality_credentials_ref:
        raise XrayProfileRenderError('transit.reality_credentials_ref is required')
    server_names = config.effective_reality_server_names()
    try:
        credentials = (
            ensure_reality_credentials(secret_store, config.transit.reality_credentials_ref, server_names=server_names)
            if ensure_missing
            else read_reality_credentials(secret_store, config.transit.reality_credentials_ref)
        )
    except CredentialError as exc:
        raise XrayProfileRenderError(str(exc)) from exc
    if tuple(server_names) != credentials.server_names:
        raise XrayProfileRenderError('transit reality secret serverNames must match foreign node serverNames')
    return credentials


def _sniffing() -> dict[str, Any]:
    return {
        'enabled': True,
        'destOverride': ['http', 'tls', 'quic'],
        'routeOnly': True,
    }


def _profile_name(config: NodeConfig) -> str:
    raw = f'tpl {config.display.internal_name}'
    cleaned = ''.join(ch if ch.isalnum() or ch in {'_', '-', ' '} else '-' for ch in raw)
    cleaned = ' '.join(cleaned.split())
    if len(cleaned) <= 30:
        return cleaned
    suffix = hashlib.sha256(config.display.internal_name.encode('utf-8')).hexdigest()[:6]
    return f'{cleaned[:23].rstrip()} {suffix}'
