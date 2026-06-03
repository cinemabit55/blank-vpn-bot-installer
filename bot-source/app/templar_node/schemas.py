"""Pydantic schema for Templar node onboarding YAML files."""

from __future__ import annotations

import re
from enum import StrEnum
from ipaddress import ip_address
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


DOMAIN_RE = re.compile(
    r'^(?=.{1,253}$)(?!-)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$',
    re.IGNORECASE,
)
INTERNAL_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,62}$')
SECRET_REF_PREFIX = 'secrets/'
VISION_FLOW = 'xtls-rprx-vision'
DEFAULT_REALITY_CLIENT_FINGERPRINT = 'firefox'


class NodeRole(StrEnum):
    FOREIGN_EXIT = 'foreign-exit'
    RU_EDGE = 'ru-edge'
    RU_WARP = 'ru-warp'


class RealityStrategy(StrEnum):
    LOCAL_DECOY_SITE = 'local_decoy_site'
    REMOTE_DEST = 'remote_dest'


class RealityTransport(StrEnum):
    TCP = 'tcp'
    XHTTP = 'xhttp'


class XhttpMode(StrEnum):
    AUTO = 'auto'
    PACKET_UP = 'packet-up'
    STREAM_UP = 'stream-up'
    STREAM_ONE = 'stream-one'


class WarpMode(StrEnum):
    DISABLED = 'disabled'
    XRAY_NATIVE = 'xray_native'
    OS_POLICY = 'os_policy'


class TransitMode(StrEnum):
    DISABLED = 'disabled'
    VLESS_REALITY = 'vless_reality'
    WIREGUARD = 'wireguard'


class DefaultRoute(StrEnum):
    DIRECT = 'direct'
    FOREIGN_EXIT = 'foreign_exit'
    BLOCK = 'block'


class CertificateMode(StrEnum):
    DNS01 = 'dns01'
    HTTP01 = 'http01'
    FILE = 'file'


class CertificateCA(StrEnum):
    PUBLIC = 'public'


class SiteEngine(StrEnum):
    CADDY = 'caddy'


class RuRoute(StrEnum):
    DIRECT = 'direct'


class TemplarBaseModel(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)


def _validate_domain(value: str) -> str:
    normalized = value.strip().rstrip('.').lower()
    if not DOMAIN_RE.fullmatch(normalized):
        raise ValueError(f'invalid domain: {value!r}')
    return normalized


def _validate_ip(value: str, *, version: int | None = None) -> str:
    parsed = ip_address(value)
    if version is not None and parsed.version != version:
        raise ValueError(f'expected IPv{version} address')
    return str(parsed)


def _validate_domain_or_ip(value: str) -> str:
    try:
        return _validate_ip(value)
    except ValueError:
        return _validate_domain(value)


def _validate_secret_ref(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith(SECRET_REF_PREFIX):
        raise ValueError(f'secret ref must start with {SECRET_REF_PREFIX!r}')
    if normalized == SECRET_REF_PREFIX:
        raise ValueError('secret ref must include a name')
    return normalized


class DisplayConfig(TemplarBaseModel):
    internal_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    cabinet_override: str | None = None

    @field_validator('internal_name')
    @classmethod
    def validate_internal_name(cls, value: str) -> str:
        stripped = value.strip()
        if not INTERNAL_NAME_RE.fullmatch(stripped):
            raise ValueError(
                'internal_name must match ^[A-Za-z][A-Za-z0-9_-]{0,62}$ '
                '(no path separators, no leading digit/hyphen)',
            )
        return stripped

    @field_validator('name', 'cabinet_override')
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError('value cannot be blank')
        return stripped


class DomainRotationConfig(TemplarBaseModel):
    spare_domains: list[str] = Field(min_length=1)
    dns_ttl_seconds: int = Field(default=300, ge=60, le=3600)

    @field_validator('spare_domains')
    @classmethod
    def validate_spare_domains(cls, value: list[str]) -> list[str]:
        normalized = [_validate_domain(item) for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError('spare_domains must be unique')
        return normalized


class MainServerConfig(TemplarBaseModel):
    ipv4: str
    remnawave_api_url: HttpUrl

    @field_validator('ipv4')
    @classmethod
    def validate_ipv4(cls, value: str) -> str:
        return _validate_ip(value, version=4)


class SshConfig(TemplarBaseModel):
    port: int = Field(default=22, ge=1, le=65535)
    admin_user: str = Field(default='templar', min_length=1)
    admin_allowlist: list[str] = Field(min_length=1)

    @field_validator('admin_user')
    @classmethod
    def validate_admin_user(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', normalized):
            raise ValueError('admin_user must be a valid Linux username')
        return normalized

    @field_validator('admin_allowlist')
    @classmethod
    def validate_admin_allowlist(cls, value: list[str]) -> list[str]:
        normalized = [_validate_ip(item, version=4) for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError('admin_allowlist must be unique')
        return normalized


class RemnaNodeConfig(TemplarBaseModel):
    node_port: int = Field(default=2222, ge=1, le=65535)
    secret_key_ref: str

    @field_validator('secret_key_ref')
    @classmethod
    def validate_secret_key_ref(cls, value: str) -> str:
        return _validate_secret_ref(value)


class XrayConfig(TemplarBaseModel):
    xtls_api_port: int = Field(default=61000, ge=1, le=65535)
    config_profile_uuid: str | None = None
    public_inbound_uuid: str

    @field_validator('config_profile_uuid', 'public_inbound_uuid')
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError('uuid/ref value cannot be blank')
        return stripped


class HostConfig(TemplarBaseModel):
    remark: str | None = None
    address: str | None = None
    port: int = Field(default=443, ge=1, le=65535)
    inbound_ref: Literal['public', 'transit'] = 'public'
    visibility: bool = True

    @field_validator('remark')
    @classmethod
    def strip_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError('remark cannot be blank')
        return stripped

    @field_validator('address')
    @classmethod
    def validate_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_domain_or_ip(value)


class XhttpConfig(TemplarBaseModel):
    path: str = Field(min_length=1)
    mode: XhttpMode = XhttpMode.AUTO
    host: str | None = None

    @field_validator('path')
    @classmethod
    def validate_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith('/'):
            raise ValueError('xhttp.path must start with /')
        if any(char in stripped for char in ('?', '#', ' ')):
            raise ValueError('xhttp.path must not contain spaces, ? or #')
        return stripped

    @field_validator('host')
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return _validate_domain(stripped)


class RealityConfig(TemplarBaseModel):
    strategy: RealityStrategy = RealityStrategy.LOCAL_DECOY_SITE
    transport: RealityTransport = RealityTransport.TCP
    client_fingerprint: str = DEFAULT_REALITY_CLIENT_FINGERPRINT
    credentials_ref: str | None = None
    public_port: int = Field(default=443, ge=1, le=65535)
    local_decoy_addr: str = '127.0.0.1'
    local_decoy_port: int = Field(default=8443, ge=1, le=65535)
    flow: str = VISION_FLOW
    server_names: list[str] | None = None
    target: str | None = None
    xhttp: XhttpConfig | None = None

    @field_validator('local_decoy_addr')
    @classmethod
    def validate_local_decoy_addr(cls, value: str) -> str:
        return _validate_ip(value)

    @field_validator('client_fingerprint')
    @classmethod
    def validate_client_fingerprint(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r'[a-z0-9-]{1,32}', normalized):
            raise ValueError('client_fingerprint must be a lowercase uTLS fingerprint name')
        return normalized

    @field_validator('server_names')
    @classmethod
    def validate_server_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [_validate_domain(item) for item in value]
        if not normalized:
            raise ValueError('server_names cannot be empty when provided')
        if len(set(normalized)) != len(normalized):
            raise ValueError('server_names must be unique')
        return normalized

    @field_validator('target')
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        host, separator, port = stripped.partition(':')
        if separator != ':' or not port.isdigit():
            raise ValueError('target must have host:port shape')
        _validate_domain(host)
        port_int = int(port)
        if port_int < 1 or port_int > 65535:
            raise ValueError('target port is out of range')
        return f'{host.lower()}:{port_int}'

    @field_validator('credentials_ref')
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_secret_ref(value)

    @model_validator(mode='after')
    def validate_strategy(self) -> RealityConfig:
        if self.flow != VISION_FLOW:
            raise ValueError(f'reality.flow must be {VISION_FLOW!r}')
        if self.transport == RealityTransport.XHTTP and self.xhttp is None:
            raise ValueError('reality.transport=xhttp requires reality.xhttp')
        if self.transport != RealityTransport.XHTTP and self.xhttp is not None:
            raise ValueError('reality.xhttp requires reality.transport=xhttp')
        if self.strategy == RealityStrategy.REMOTE_DEST:
            if self.target is None:
                raise ValueError('remote_dest strategy requires reality.target')
            if not self.server_names:
                raise ValueError('remote_dest strategy requires reality.server_names')
        return self


class SiteConfig(TemplarBaseModel):
    engine: SiteEngine = SiteEngine.CADDY
    template: str = Field(default='simple-studio', min_length=1)
    title: str = Field(min_length=1)
    contact_email: str = Field(min_length=3)
    certificate_mode: CertificateMode = CertificateMode.FILE
    certificate_source: str | None = None
    certificate_ca: CertificateCA = CertificateCA.PUBLIC
    dns_api_token_ref: str | None = None

    @field_validator('template', 'title', 'contact_email', 'certificate_source')
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError('value cannot be blank')
        return stripped

    @field_validator('dns_api_token_ref')
    @classmethod
    def validate_dns_api_token_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_secret_ref(value)

    @model_validator(mode='after')
    def validate_certificate_mode(self) -> SiteConfig:
        if self.certificate_mode == CertificateMode.FILE and self.certificate_source is None:
            raise ValueError('certificate_mode=file requires certificate_source')
        if self.certificate_mode == CertificateMode.DNS01 and self.dns_api_token_ref is None:
            raise ValueError('certificate_mode=dns01 requires dns_api_token_ref')
        if self.certificate_ca != CertificateCA.PUBLIC:
            raise ValueError('site.certificate_ca must be public')
        return self


class WarpConfig(TemplarBaseModel):
    mode: WarpMode
    outbound_tag: str | None = None
    reserved_source: str | None = None
    reserved: list[int] | None = None
    registration_ref: str | None = None
    discord_direct: bool = True

    @field_validator('outbound_tag', 'reserved_source')
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError('value cannot be blank')
        return stripped

    @field_validator('registration_ref')
    @classmethod
    def validate_registration_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_secret_ref(value)

    @field_validator('reserved')
    @classmethod
    def validate_reserved(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if len(value) != 3 or any(item < 0 or item > 255 for item in value):
            raise ValueError('warp.reserved must contain exactly three bytes')
        return value

    @model_validator(mode='after')
    def validate_mode(self) -> WarpConfig:
        if self.mode == WarpMode.XRAY_NATIVE:
            if self.outbound_tag is None:
                raise ValueError('xray_native WARP requires outbound_tag')
            if self.registration_ref is None:
                raise ValueError('xray_native WARP requires registration_ref')
            if self.reserved is None and self.reserved_source != 'warp_registration_client_id_b64':
                raise ValueError('xray_native WARP requires reserved or reserved_source=warp_registration_client_id_b64')
        if self.mode == WarpMode.DISABLED and any(
            item is not None for item in (self.outbound_tag, self.reserved_source, self.reserved, self.registration_ref)
        ):
            raise ValueError('disabled WARP must not define outbound fields')
        return self


class BackupTransitOutbound(TemplarBaseModel):
    tag: str = Field(min_length=1)
    domain: str
    port: int = Field(default=10443, ge=1, le=65535)
    server_names: list[str] = Field(min_length=1)
    service_user_credential_ref: str
    reality_credentials_ref: str

    @field_validator('domain')
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _validate_domain(value)

    @field_validator('server_names')
    @classmethod
    def validate_server_names(cls, value: list[str]) -> list[str]:
        return [_validate_domain(item) for item in value]

    @field_validator('service_user_credential_ref', 'reality_credentials_ref')
    @classmethod
    def validate_secret_refs(cls, value: str) -> str:
        return _validate_secret_ref(value)


class TransitConfig(TemplarBaseModel):
    mode: TransitMode
    inbound_tag: str | None = None
    listen_port: int | None = None
    allow_from: list[str] | None = None
    flow: str | None = None
    outbound_tag: str | None = None
    foreign_exit_domain: str | None = None
    foreign_exit_port: int | None = None
    server_names: list[str] | None = None
    service_user: str | None = None
    service_user_credential_ref: str | None = None
    reality_credentials_ref: str | None = None
    backup_outbounds: list[BackupTransitOutbound] = Field(default_factory=list)

    @field_validator('inbound_tag', 'outbound_tag', 'service_user', 'flow')
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError('value cannot be blank')
        return stripped

    @field_validator('listen_port', 'foreign_exit_port')
    @classmethod
    def validate_port(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1 or value > 65535:
            raise ValueError('port is out of range')
        return value

    @field_validator('allow_from')
    @classmethod
    def validate_allow_from(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [_validate_ip(item) for item in value]
        if not normalized:
            raise ValueError('allow_from cannot be empty when provided')
        return normalized

    @model_validator(mode='after')
    def validate_disabled_mode(self) -> TransitConfig:
        if self.mode != TransitMode.DISABLED:
            return self
        populated = [
            name
            for name, value in {
                'inbound_tag': self.inbound_tag,
                'listen_port': self.listen_port,
                'allow_from': self.allow_from,
                'flow': self.flow,
                'outbound_tag': self.outbound_tag,
                'foreign_exit_domain': self.foreign_exit_domain,
                'foreign_exit_port': self.foreign_exit_port,
                'server_names': self.server_names,
                'service_user': self.service_user,
                'service_user_credential_ref': self.service_user_credential_ref,
                'reality_credentials_ref': self.reality_credentials_ref,
                'backup_outbounds': self.backup_outbounds or None,
            }.items()
            if value not in (None, [], '')
        ]
        if populated:
            raise ValueError(
                f'transit.mode=disabled must not define any other transit fields: {", ".join(populated)}',
            )
        return self

    @field_validator('foreign_exit_domain')
    @classmethod
    def validate_foreign_exit_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_domain_or_ip(value)

    @field_validator('server_names')
    @classmethod
    def validate_server_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [_validate_domain(item) for item in value]
        if not normalized:
            raise ValueError('server_names cannot be empty when provided')
        return normalized

    @field_validator('service_user_credential_ref', 'reality_credentials_ref')
    @classmethod
    def validate_secret_refs(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_secret_ref(value)


class RoutingConfig(TemplarBaseModel):
    ru_route: RuRoute = RuRoute.DIRECT
    default_route: DefaultRoute
    ru_dns: list[str] = Field(default_factory=list)
    foreign_dns_via_transit: bool = True

    @field_validator('ru_dns')
    @classmethod
    def validate_ru_dns(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError('ru_dns cannot contain blank values')
        return normalized


class BedolagaConfig(TemplarBaseModel):
    internal_squad_name: str = Field(min_length=1)
    external_squad_name: str = Field(min_length=1)
    attach_to_tariff_slugs: list[str] = Field(default_factory=list)
    attach_to_tariff_names: list[str] = Field(default_factory=list)
    trial_eligible: bool = False

    @field_validator('internal_squad_name', 'external_squad_name')
    @classmethod
    def strip_names(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError('value cannot be blank')
        return stripped

    @field_validator('attach_to_tariff_slugs', 'attach_to_tariff_names')
    @classmethod
    def validate_tariff_refs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError('tariff refs cannot contain blank values')
        if len(set(normalized)) != len(normalized):
            raise ValueError('tariff refs must be unique')
        return normalized

    @model_validator(mode='after')
    def validate_tariff_targets(self) -> BedolagaConfig:
        if not self.attach_to_tariff_slugs and not self.attach_to_tariff_names and not self.trial_eligible:
            raise ValueError('at least one tariff slug/name or trial eligibility is required')
        return self


class NodeConfig(TemplarBaseModel):
    schema_version: Literal[1]
    role: NodeRole
    display: DisplayConfig
    country_code: str
    domain: str
    public_ipv4: str
    public_ipv6: str | None = None
    domain_rotation: DomainRotationConfig
    main_server: MainServerConfig
    ssh: SshConfig
    remnanode: RemnaNodeConfig
    xray: XrayConfig
    host: HostConfig
    reality: RealityConfig
    site: SiteConfig
    warp: WarpConfig
    transit: TransitConfig
    bedolaga: BedolagaConfig
    routing: RoutingConfig | None = None

    @field_validator('country_code')
    @classmethod
    def validate_country_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r'[A-Z]{2}', normalized):
            raise ValueError('country_code must be ISO-3166 alpha-2')
        return normalized

    @field_validator('domain')
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _validate_domain(value)

    @field_validator('public_ipv4')
    @classmethod
    def validate_public_ipv4(cls, value: str) -> str:
        return _validate_ip(value, version=4)

    @field_validator('public_ipv6')
    @classmethod
    def validate_public_ipv6(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_ip(value, version=6)

    @model_validator(mode='after')
    def validate_node_contract(self) -> NodeConfig:
        self._validate_host_defaults()
        self._validate_reality_contract()
        self._validate_site_contract()
        self._validate_role_contract()
        return self

    def effective_host_address(self) -> str:
        return self.host.address or self.domain

    def effective_host_remark(self) -> str:
        return self.host.remark or self.display.name

    def effective_cabinet_name(self) -> str:
        return self.display.cabinet_override or self.display.name

    def effective_reality_server_names(self) -> list[str]:
        return self.reality.server_names or [self.domain]

    def secret_refs(self) -> list[str]:
        refs = [
            self.remnanode.secret_key_ref,
            self.reality.credentials_ref,
            self.site.dns_api_token_ref,
            self.warp.registration_ref,
            self.transit.service_user_credential_ref,
            self.transit.reality_credentials_ref,
        ]
        refs.extend(item.service_user_credential_ref for item in self.transit.backup_outbounds)
        refs.extend(item.reality_credentials_ref for item in self.transit.backup_outbounds)
        return sorted({item for item in refs if item})

    def _validate_host_defaults(self) -> None:
        if self.host.address is not None and self.host.address != self.domain:
            _validate_domain_or_ip(self.host.address)
        if self.host.inbound_ref == 'public':
            if self.host.port != self.reality.public_port:
                raise ValueError('host.port must match reality.public_port')
            return
        if self.role != NodeRole.FOREIGN_EXIT or self.transit.mode != TransitMode.VLESS_REALITY or not self.transit.listen_port:
            raise ValueError('host.inbound_ref=transit requires a foreign-exit VLESS transit inbound')
        if self.host.port != self.transit.listen_port:
            raise ValueError('host.port must match transit.listen_port when host.inbound_ref=transit')

    def _validate_reality_contract(self) -> None:
        server_names = self.effective_reality_server_names()
        if self.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE and self.domain not in server_names:
            raise ValueError('local_decoy_site server_names must include top-level domain')

    def _validate_site_contract(self) -> None:
        if self.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE and self.site.engine != SiteEngine.CADDY:
            raise ValueError('local_decoy_site currently requires Caddy')

    def _validate_role_contract(self) -> None:
        if self.role == NodeRole.FOREIGN_EXIT:
            self._validate_foreign_exit()
        elif self.role == NodeRole.RU_EDGE:
            self._validate_ru_edge()
        elif self.role == NodeRole.RU_WARP:
            self._validate_ru_warp()

    def _validate_foreign_exit(self) -> None:
        if self.warp.mode != WarpMode.XRAY_NATIVE:
            raise ValueError('foreign-exit requires warp.mode=xray_native')
        if self.transit.mode != TransitMode.VLESS_REALITY:
            raise ValueError('foreign-exit requires transit.mode=vless_reality')
        required: dict[str, Any] = {
            'transit.inbound_tag': self.transit.inbound_tag,
            'transit.listen_port': self.transit.listen_port,
            'transit.flow': self.transit.flow,
            'transit.allow_from': self.transit.allow_from,
            'transit.service_user': self.transit.service_user,
            'transit.service_user_credential_ref': self.transit.service_user_credential_ref,
            'transit.reality_credentials_ref': self.transit.reality_credentials_ref,
        }
        self._require_fields(required)
        if self.transit.flow != VISION_FLOW:
            raise ValueError(f'foreign-exit transit.flow must be {VISION_FLOW!r}')

    def _validate_ru_edge(self) -> None:
        if self.warp.mode != WarpMode.DISABLED:
            raise ValueError('ru-edge requires warp.mode=disabled')
        if self.transit.mode != TransitMode.VLESS_REALITY:
            raise ValueError('ru-edge requires transit.mode=vless_reality')
        if self.routing is None:
            raise ValueError('ru-edge requires routing section')
        if self.routing.default_route != DefaultRoute.FOREIGN_EXIT:
            raise ValueError('ru-edge requires routing.default_route=foreign_exit')
        if not self.routing.foreign_dns_via_transit:
            raise ValueError('routing.foreign_dns_via_transit=false is forbidden for ru-edge cascade')
        if not self.routing.ru_dns:
            raise ValueError('ru-edge routing.ru_dns must not be empty')
        required: dict[str, Any] = {
            'transit.outbound_tag': self.transit.outbound_tag,
            'transit.foreign_exit_domain': self.transit.foreign_exit_domain,
            'transit.foreign_exit_port': self.transit.foreign_exit_port,
            'transit.server_names': self.transit.server_names,
            'transit.service_user': self.transit.service_user,
            'transit.service_user_credential_ref': self.transit.service_user_credential_ref,
            'transit.reality_credentials_ref': self.transit.reality_credentials_ref,
        }
        self._require_fields(required)

    def _validate_ru_warp(self) -> None:
        if self.warp.mode != WarpMode.XRAY_NATIVE:
            raise ValueError('ru-warp requires warp.mode=xray_native')
        if self.transit.mode != TransitMode.DISABLED:
            raise ValueError('ru-warp requires transit.mode=disabled')
        if self.routing is not None and self.routing.default_route == DefaultRoute.FOREIGN_EXIT:
            raise ValueError('ru-warp cannot use routing.default_route=foreign_exit')

    @staticmethod
    def _require_fields(fields: dict[str, Any]) -> None:
        missing = [name for name, value in fields.items() if value is None or value == []]
        if missing:
            raise ValueError(f'missing required fields: {", ".join(missing)}')
