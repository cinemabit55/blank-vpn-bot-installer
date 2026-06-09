"""RemnaWave control-plane adapter contracts."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.templar_node.credentials import CredentialError, ensure_vless_uuid
from app.templar_node.fake_env import FakeEnvironmentStore, ensure_record, fake_uuid
from app.templar_node.http_json import JsonHttpClient, JsonHttpError
from app.templar_node.remnawave_probe import RemnaWaveProbeAuth, fetch_remnawave_node_secret_key
from app.templar_node.schemas import NodeConfig, NodeRole, RealityTransport
from app.templar_node.state import utc_now_iso
from app.templar_node.xray_profile import (
    PUBLIC_INBOUND_TAG,
    XrayProfileRenderError,
    public_inbound_tag,
    render_xray_config_profile,
)


if TYPE_CHECKING:
    from app.templar_node.routes import RouteOverrides
    from app.templar_node.secrets import LocalSecretStore as LocalSecretStoreLike


class RemnaWaveAdapterError(RuntimeError):
    """Raised when a RemnaWave adapter cannot complete a control-plane action."""


TRANSIT_SERVICE_USER_TRAFFIC_LIMIT_BYTES = 10 * 1024**5
HOST_REALITY_FINGERPRINT = 'firefox'
REMNAWAVE_USERS_PAGE_SIZE = '1000'


@dataclass(frozen=True)
class ConfigProfileRecord:
    uuid: str
    status: str


@dataclass(frozen=True)
class RemnaWaveNodeRecord:
    uuid: str
    secret_key: str
    status: str


@dataclass(frozen=True)
class HostRecord:
    uuid: str
    status: str


@dataclass(frozen=True)
class InternalSquadRecord:
    uuid: str
    status: str


@dataclass(frozen=True)
class ExternalSquadRecord:
    uuid: str
    status: str


@dataclass(frozen=True)
class ServiceUserRecord:
    uuid: str
    status: str


@dataclass(frozen=True)
class ProfileUpdateRecord:
    key: str
    status: str


@dataclass(frozen=True)
class RemnaWaveDeleteAction:
    kind: str
    identifier: str
    status: str
    detail: str = ''


@dataclass(frozen=True)
class RemnaWaveDecommissionResult:
    actions: tuple[RemnaWaveDeleteAction, ...]

    def to_lines(self) -> list[str]:
        lines = ['RemnaWave cleanup:']
        lines.extend(
            f'- {action.kind} {action.identifier}: {action.status}' + (f' ({action.detail})' if action.detail else '')
            for action in self.actions
        )
        return lines


class RemnaWaveControlPlaneAdapter(Protocol):
    def ensure_config_profile(self, config: NodeConfig, discovered_uuid: str | None = None) -> ConfigProfileRecord:
        """Create/find the config profile required by a node."""

    def ensure_node(self, config: NodeConfig) -> RemnaWaveNodeRecord:
        """Create/find the RemnaWave Node and return its SECRET_KEY."""

    def ensure_node_online(self, config: NodeConfig) -> None:
        """Verify that a previously registered Node is online."""

    def ensure_host(self, config: NodeConfig) -> HostRecord:
        """Create/find a Host for the public inbound."""

    def ensure_internal_squad(self, config: NodeConfig, *, host_uuid: str) -> InternalSquadRecord:
        """Create/find an Internal Squad for the Host."""

    def ensure_external_squad(self, config: NodeConfig, *, internal_squad_uuid: str) -> ExternalSquadRecord:
        """Create/find an External Squad for Bedolaga/cabinet grouping."""

    def ensure_transit_service_user(self, config: NodeConfig, *, internal_squad_uuid: str | None = None) -> ServiceUserRecord | None:
        """Create/find the system transit user when the node needs one."""

    def ensure_profile_update(
        self,
        config: NodeConfig,
        *,
        profile_uuid: str,
        route_overrides: RouteOverrides | None = None,
    ) -> ProfileUpdateRecord:
        """Apply/update the config profile snippet for a node."""


class LocalRemnaWaveAdapter:
    """Local fake RemnaWave adapter backed by FakeEnvironmentStore.

    This adapter is intentionally deterministic and idempotent. It gives us a
    safe pre-bootstrap runner before real API endpoints are wired.

    SECRET_KEY for the RemnaWave Node is NEVER persisted in the env-json record.
    When ``secret_store`` is provided, ``ensure_node`` reads the existing
    SECRET_KEY from it on re-runs so that the value stays stable. Layer 2a
    binds its secret store automatically for the legacy
    ``LocalRemnaWaveAdapter(env_store)`` construction style.
    """

    def __init__(self, env_store: FakeEnvironmentStore, secret_store: LocalSecretStoreLike | None = None):
        self.env_store = env_store
        self.secret_store = secret_store

    def ensure_config_profile(self, config: NodeConfig, discovered_uuid: str | None = None) -> ConfigProfileRecord:
        environment = self.env_store.load()
        profile_uuid = (
            config.xray.config_profile_uuid
            or discovered_uuid
            or fake_uuid('config-profile', f'{config.role.value}:{config.display.internal_name}')
        )
        record = {
            'uuid': profile_uuid,
            'name': f'{config.role.value}:{config.display.internal_name}',
            'role': config.role.value,
        }
        status = ensure_record(environment['remnawave']['config_profiles'], profile_uuid, record)
        self.env_store.save(environment)
        return ConfigProfileRecord(uuid=profile_uuid, status=status)

    def ensure_node(self, config: NodeConfig) -> RemnaWaveNodeRecord:
        environment = self.env_store.load()
        node_uuid = fake_uuid('remnawave-node', config.display.internal_name)
        existing = environment['remnawave']['nodes'].get(config.display.internal_name)
        secret_key = self._resolve_secret_key(config)
        record = {
            'uuid': node_uuid,
            'internal_name': config.display.internal_name,
            'role': config.role.value,
            'country_code': config.country_code,
            'domain': config.domain,
            'public_ipv4': config.public_ipv4,
            'node_port': config.remnanode.node_port,
            'secret_key_ref': config.remnanode.secret_key_ref,
            'online': bool(existing.get('online')) if existing else False,
            'updated_at': utc_now_iso(),
        }
        status = ensure_record(environment['remnawave']['nodes'], config.display.internal_name, record)
        self.env_store.save(environment)
        return RemnaWaveNodeRecord(uuid=node_uuid, secret_key=secret_key, status=status)

    def _resolve_secret_key(self, config: NodeConfig) -> str:
        """Reuse the existing SECRET_KEY from secret store if present; else generate one.

        The SECRET_KEY value MUST NOT be persisted in the env-json record. It lives
        only inside the secret store file (chmod 0o600) referenced by
        ``remnanode.secret_key_ref``.
        """
        if self.secret_store is not None:
            check = self.secret_store.check_ref(config.remnanode.secret_key_ref)
            if check.exists and check.readable:
                try:
                    existing_value = self.secret_store.read_text(config.remnanode.secret_key_ref)
                except Exception:
                    existing_value = ''
                if existing_value:
                    return existing_value
        return _new_secret_key()

    def ensure_node_online(self, config: NodeConfig) -> None:
        environment = self.env_store.load()
        node = environment['remnawave']['nodes'].get(config.display.internal_name)
        if not node:
            raise RemnaWaveAdapterError(f'RemnaWave Node {config.display.internal_name!r} does not exist; run pre-bootstrap first')
        if not node.get('online'):
            raise RemnaWaveAdapterError(f'RemnaWave Node {config.display.internal_name!r} is not online; run bootstrap first')

    def ensure_host(self, config: NodeConfig) -> HostRecord:
        environment = self.env_store.load()
        host_uuid = fake_uuid('host', config.display.internal_name)
        record = {
            'uuid': host_uuid,
            'node': config.display.internal_name,
            'address': config.effective_host_address(),
            'port': config.host.port,
            'remark': config.effective_host_remark(),
            'display_name': config.effective_cabinet_name(),
            'visibility': config.host.visibility,
            'inbound_ref': config.host.inbound_ref,
        }
        status = ensure_record(environment['remnawave']['hosts'], config.display.internal_name, record)
        self.env_store.save(environment)
        return HostRecord(uuid=host_uuid, status=status)

    def ensure_internal_squad(self, config: NodeConfig, *, host_uuid: str) -> InternalSquadRecord:
        environment = self.env_store.load()
        internal_squad_uuid = fake_uuid('internal-squad', config.bedolaga.internal_squad_name)
        record = {
            'uuid': internal_squad_uuid,
            'name': config.bedolaga.internal_squad_name,
            'host_uuid': host_uuid,
            'node': config.display.internal_name,
        }
        status = ensure_record(environment['remnawave']['internal_squads'], config.bedolaga.internal_squad_name, record)
        self.env_store.save(environment)
        return InternalSquadRecord(uuid=internal_squad_uuid, status=status)

    def ensure_external_squad(self, config: NodeConfig, *, internal_squad_uuid: str) -> ExternalSquadRecord:
        environment = self.env_store.load()
        external_squad_uuid = fake_uuid('external-squad', config.bedolaga.external_squad_name)
        record = {
            'uuid': external_squad_uuid,
            'name': config.bedolaga.external_squad_name,
            'internal_squad_uuids': [internal_squad_uuid],
        }
        status = ensure_record(environment['remnawave']['external_squads'], config.bedolaga.external_squad_name, record)
        self.env_store.save(environment)
        return ExternalSquadRecord(uuid=external_squad_uuid, status=status)

    def ensure_transit_service_user(self, config: NodeConfig, *, internal_squad_uuid: str | None = None) -> ServiceUserRecord | None:
        if not config.transit.service_user:
            return None
        environment = self.env_store.load()
        key = f'system:transit:{config.transit.service_user}'
        existing = environment['remnawave']['service_users'].get(key)
        credential_ref = config.transit.service_user_credential_ref
        if existing is not None and existing.get('credential_ref') != credential_ref:
            raise RemnaWaveAdapterError(
                f'service user {config.transit.service_user!r} already exists with a different credential ref; '
                'manual cleanup is required',
            )
        service_user_uuid = fake_uuid('service-user', key)
        record = {
            'uuid': service_user_uuid,
            'username': config.transit.service_user,
            'tag': 'system:transit',
            'credential_ref': credential_ref,
            'status': 'ACTIVE',
            'expire_at': None,
            'traffic_limit_bytes': TRANSIT_SERVICE_USER_TRAFFIC_LIMIT_BYTES,
        }
        status = ensure_record(environment['remnawave']['service_users'], key, record)
        self.env_store.save(environment)
        return ServiceUserRecord(uuid=service_user_uuid, status=status)

    def ensure_profile_update(
        self,
        config: NodeConfig,
        *,
        profile_uuid: str,
        route_overrides: RouteOverrides | None = None,
    ) -> ProfileUpdateRecord:
        environment = self.env_store.load()
        key = f'{profile_uuid}:{config.display.internal_name}'
        record = {
            'profile_uuid': profile_uuid,
            'node': config.display.internal_name,
            'role': config.role.value,
            'public_inbound_uuid': config.xray.public_inbound_uuid,
            'transit_mode': config.transit.mode.value,
            'warp_mode': config.warp.mode.value,
        }
        if route_overrides is not None:
            record['route_overrides'] = {
                'domains': list(route_overrides.domains),
                'ips': list(route_overrides.ips),
            }
        status = ensure_record(environment['remnawave']['profile_updates'], key, record)
        self.env_store.save(environment)
        return ProfileUpdateRecord(key=key, status=status)

    def decommission_resources(
        self,
        config: NodeConfig,
        *,
        discovered: dict[str, Any],
        delete_transit_service_user: bool,
        dry_run: bool,
    ) -> RemnaWaveDecommissionResult:
        environment = self.env_store.load()
        profile_uuid = str(discovered.get('config_profile_uuid') or config.xray.config_profile_uuid or fake_uuid('config-profile', f'{config.role.value}:{config.display.internal_name}'))
        actions = [
            _local_delete(environment['remnawave']['hosts'], config.display.internal_name, 'host', dry_run=dry_run),
            _local_delete(environment['remnawave']['internal_squads'], config.bedolaga.internal_squad_name, 'internal_squad', dry_run=dry_run),
            _local_delete(environment['remnawave']['external_squads'], config.bedolaga.external_squad_name, 'external_squad', dry_run=dry_run),
            _local_delete(environment['remnawave']['profile_updates'], str(discovered.get('profile_update_key') or f'{profile_uuid}:{config.display.internal_name}'), 'profile_update', dry_run=dry_run),
            _local_delete(environment['remnawave']['nodes'], config.display.internal_name, 'node', dry_run=dry_run),
            _local_delete(environment['remnawave']['config_profiles'], profile_uuid, 'config_profile', dry_run=dry_run),
        ]
        if delete_transit_service_user and config.transit.service_user:
            actions.append(
                _local_delete(
                    environment['remnawave']['service_users'],
                    f'system:transit:{config.transit.service_user}',
                    'transit_service_user',
                    dry_run=dry_run,
                ),
            )
        if not dry_run:
            self.env_store.save(environment)
        return RemnaWaveDecommissionResult(actions=tuple(actions))


class HttpRemnaWaveAdapter:
    """Live RemnaWave API adapter for Layer 2a/2b onboarding."""

    def __init__(
        self,
        *,
        api_url: str,
        auth: RemnaWaveProbeAuth,
        secret_store: LocalSecretStoreLike,
        timeout_seconds: int = 20,
        verify_tls: bool = True,
    ):
        self.api_url = api_url.rstrip('/')
        self.auth = auth
        self.secret_store = secret_store
        self.client = JsonHttpClient(
            base_url=self.api_url,
            headers=auth.headers(),
            timeout_seconds=timeout_seconds,
            verify_tls=verify_tls,
        )
        self._profile_cache: dict[str, dict[str, Any]] = {}
        self._node_cache: dict[str, dict[str, Any]] = {}

    def ensure_config_profile(self, config: NodeConfig, discovered_uuid: str | None = None) -> ConfigProfileRecord:
        try:
            rendered = render_xray_config_profile(config, secret_store=self.secret_store)
            existing = self._find_config_profile(config, discovered_uuid=discovered_uuid, expected_name=rendered.name)
            if existing is None:
                payload = self.client.post(
                    '/api/config-profiles',
                    json_body={'name': rendered.name, 'config': rendered.config},
                )
                profile = _response_object(payload, 'created config profile')
                status = 'created'
            else:
                profile_uuid = _required_uuid(existing, 'config profile')
                payload = self.client.patch(
                    '/api/config-profiles',
                    json_body={'uuid': profile_uuid, 'name': rendered.name, 'config': rendered.config},
                )
                profile = _response_object(payload, 'updated config profile')
                status = 'updated'
            self._profile_cache[config.display.internal_name] = profile
            return ConfigProfileRecord(uuid=_required_uuid(profile, 'config profile'), status=status)
        except (JsonHttpError, XrayProfileRenderError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'config profile sync failed: {exc}') from exc

    def ensure_node(self, config: NodeConfig) -> RemnaWaveNodeRecord:
        try:
            profile = self._get_profile(config)
            profile_uuid = _required_uuid(profile, 'config profile')
            active_inbounds = [_required_uuid(inbound, 'profile inbound') for inbound in _profile_inbounds(profile)]
            if not active_inbounds:
                raise RemnaWaveAdapterError('config profile has no inbounds after render')
            secret_key = self._get_or_create_node_secret(config)
            existing = self._find_one_by_name(self._list_nodes(), config.display.internal_name, 'RemnaWave Node')
            body = {
                'name': config.display.internal_name,
                'address': config.public_ipv4,
                'port': config.remnanode.node_port,
                'countryCode': config.country_code,
                'isTrafficTrackingActive': False,
                'trafficLimitBytes': 0,
                'notifyPercent': 0,
                'consumptionMultiplier': 1,
                'configProfile': {
                    'activeConfigProfileUuid': profile_uuid,
                    'activeInbounds': active_inbounds,
                },
                'tags': ['TEMPLAR'],
            }
            if existing is None:
                node = _response_object(self.client.post('/api/nodes', json_body=body), 'created node')
                status = 'created'
            else:
                node_uuid = _required_uuid(existing, 'node')
                node = _response_object(self.client.patch('/api/nodes', json_body={'uuid': node_uuid, **body}), 'updated node')
                status = 'updated'
            self._node_cache[config.display.internal_name] = node
            return RemnaWaveNodeRecord(uuid=_required_uuid(node, 'node'), secret_key=secret_key, status=status)
        except (JsonHttpError, RemnaWaveAdapterError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'node sync failed: {exc}') from exc

    def ensure_node_online(self, config: NodeConfig) -> None:
        node = self._find_one_by_name(self._list_nodes(), config.display.internal_name, 'RemnaWave Node')
        if node is None:
            raise RemnaWaveAdapterError(f'RemnaWave Node {config.display.internal_name!r} does not exist; run pre-bootstrap first')
        self._node_cache[config.display.internal_name] = node
        if bool(node.get('isDisabled')):
            raise RemnaWaveAdapterError(f'RemnaWave Node {config.display.internal_name!r} is disabled')
        if not bool(node.get('isConnected')):
            raise RemnaWaveAdapterError(f'RemnaWave Node {config.display.internal_name!r} is not online; run bootstrap first')

    def ensure_host(self, config: NodeConfig) -> HostRecord:
        try:
            profile = self._get_profile(config)
            host_inbound = self._find_host_profile_inbound(config, profile)
            node = self._get_node(config)
            body = {
                'inbound': {
                    'configProfileUuid': _required_uuid(profile, 'config profile'),
                    'configProfileInboundUuid': _required_uuid(host_inbound, f'{config.host.inbound_ref} inbound'),
                },
                'remark': _truncate_api_text(config.effective_host_remark(), 40),
                'address': config.effective_host_address(),
                'port': config.host.port,
                'sni': _host_sni(config),
                'isDisabled': not config.host.visibility,
                'isHidden': not config.host.visibility,
                'securityLayer': 'DEFAULT',
                'fingerprint': config.reality.client_fingerprint or HOST_REALITY_FINGERPRINT,
                'serverDescription': _truncate_host_description(config.effective_cabinet_name()),
                'tag': 'TEMPLAR',
                'nodes': [_required_uuid(node, 'node')],
            }
            if config.reality.transport == RealityTransport.XHTTP:
                if config.reality.xhttp is None:
                    raise RemnaWaveAdapterError('reality.transport=xhttp requires reality.xhttp')
                body['path'] = config.reality.xhttp.path
                body['host'] = config.reality.xhttp.host or ''
            else:
                body['path'] = ''
                body['host'] = ''
            existing = self._find_host(config)
            if existing is None:
                host = _response_object(self.client.post('/api/hosts', json_body=body), 'created host')
                status = 'created'
            else:
                host = _response_object(
                    self.client.patch('/api/hosts', json_body={'uuid': _required_uuid(existing, 'host'), **body}),
                    'updated host',
                )
                status = 'updated'
            return HostRecord(uuid=_required_uuid(host, 'host'), status=status)
        except (JsonHttpError, RemnaWaveAdapterError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'host sync failed: {exc}') from exc

    def ensure_internal_squad(self, config: NodeConfig, *, host_uuid: str) -> InternalSquadRecord:
        try:
            profile = self._get_profile(config)
            inbounds = [_required_uuid(self._find_public_profile_inbound(config, profile), 'public inbound')]
            if config.transit.inbound_tag:
                try:
                    inbounds.append(_required_uuid(self._find_profile_inbound(profile, config.transit.inbound_tag), 'transit inbound'))
                except RemnaWaveAdapterError:
                    pass
            body = {'name': _api_name(config.bedolaga.internal_squad_name), 'inbounds': sorted(set(inbounds))}
            existing = self._find_one_by_name(self._list_internal_squads(), body['name'], 'Internal Squad')
            if existing is None:
                squad = _response_object(self.client.post('/api/internal-squads', json_body=body), 'created internal squad')
                status = 'created'
            else:
                squad = _response_object(
                    self.client.patch('/api/internal-squads', json_body={'uuid': _required_uuid(existing, 'internal squad'), **body}),
                    'updated internal squad',
                )
                status = 'updated'
            return InternalSquadRecord(uuid=_required_uuid(squad, 'internal squad'), status=status)
        except (JsonHttpError, RemnaWaveAdapterError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'internal squad sync failed: {exc}') from exc

    def ensure_external_squad(self, config: NodeConfig, *, internal_squad_uuid: str) -> ExternalSquadRecord:
        try:
            name = _api_name(config.bedolaga.external_squad_name)
            existing = self._find_one_by_name(self._list_external_squads(), name, 'External Squad')
            if existing is None:
                squad = _response_object(self.client.post('/api/external-squads', json_body={'name': name}), 'created external squad')
                status = 'created'
            else:
                squad = _response_object(
                    self.client.patch('/api/external-squads', json_body={'uuid': _required_uuid(existing, 'external squad'), 'name': name}),
                    'updated external squad',
                )
                status = 'updated'
            return ExternalSquadRecord(uuid=_required_uuid(squad, 'external squad'), status=status)
        except (JsonHttpError, RemnaWaveAdapterError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'external squad sync failed: {exc}') from exc

    def ensure_transit_service_user(self, config: NodeConfig, *, internal_squad_uuid: str | None = None) -> ServiceUserRecord | None:
        if not config.transit.service_user:
            return None
        if not (config.transit.inbound_tag and config.transit.listen_port):
            existing = self._find_service_user(config)
            if existing is None:
                raise RemnaWaveAdapterError('transit service user must be created by the transit-inbound phase first')
            return ServiceUserRecord(uuid=_required_uuid(existing, 'service user'), status='existing')
        try:
            existing = self._find_service_user(config)
            credential_ref = config.transit.service_user_credential_ref
            if not credential_ref:
                raise RemnaWaveAdapterError('transit service user requires service_user_credential_ref')
            if existing is not None:
                self._sync_existing_service_user_secret(config, existing)
                body = {
                    'uuid': _required_uuid(existing, 'service user'),
                    'activeInternalSquads': _merged_squad_uuids(existing, internal_squad_uuid),
                    'status': 'ACTIVE',
                    'trafficLimitBytes': TRANSIT_SERVICE_USER_TRAFFIC_LIMIT_BYTES,
                    'trafficLimitStrategy': 'NO_RESET',
                }
                user = _response_object(self.client.patch('/api/users', json_body=body), 'updated service user')
                return ServiceUserRecord(uuid=_required_uuid(user, 'service user'), status='updated')
            vless_uuid = ensure_vless_uuid(self.secret_store, credential_ref)
            body = {
                'username': config.transit.service_user,
                'status': 'ACTIVE',
                'vlessUuid': vless_uuid,
                'trafficLimitBytes': TRANSIT_SERVICE_USER_TRAFFIC_LIMIT_BYTES,
                'trafficLimitStrategy': 'NO_RESET',
                'expireAt': '2099-01-01T00:00:00.000Z',
                'description': f'Templar transit service user for {config.display.internal_name}',
                'tag': 'SYSTEM_TRANSIT',
                'activeInternalSquads': [internal_squad_uuid] if internal_squad_uuid else [],
            }
            user = _response_object(self.client.post('/api/users', json_body=body), 'created service user')
            return ServiceUserRecord(uuid=_required_uuid(user, 'service user'), status='created')
        except (JsonHttpError, RemnaWaveAdapterError, CredentialError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'transit service user sync failed: {exc}') from exc

    def ensure_profile_update(
        self,
        config: NodeConfig,
        *,
        profile_uuid: str,
        route_overrides: RouteOverrides | None = None,
    ) -> ProfileUpdateRecord:
        try:
            rendered = render_xray_config_profile(
                config,
                secret_store=self.secret_store,
                ensure_missing=False,
                route_overrides=route_overrides,
            )
            payload = self.client.patch(
                '/api/config-profiles',
                json_body={'uuid': profile_uuid, 'name': rendered.name, 'config': rendered.config},
            )
            profile = _response_object(payload, 'updated config profile')
            self._profile_cache[config.display.internal_name] = profile
            return ProfileUpdateRecord(key=f'{profile_uuid}:{config.display.internal_name}', status='updated')
        except (JsonHttpError, XrayProfileRenderError, ValueError) as exc:
            raise RemnaWaveAdapterError(f'profile update failed: {exc}') from exc

    def decommission_resources(
        self,
        config: NodeConfig,
        *,
        discovered: dict[str, Any],
        delete_transit_service_user: bool,
        dry_run: bool,
    ) -> RemnaWaveDecommissionResult:
        actions: list[RemnaWaveDeleteAction] = []
        profile = self._find_config_profile(
            config,
            discovered_uuid=str(discovered.get('config_profile_uuid') or ''),
            expected_name=_profile_name(config),
        )
        host = self._find_host_by_uuid_or_config(config, str(discovered.get('host_uuid') or ''))
        internal = self._find_one_by_uuid_or_name(
            self._list_internal_squads(),
            str(discovered.get('internal_squad_uuid') or ''),
            _api_name(config.bedolaga.internal_squad_name),
            'Internal Squad',
        )
        external = self._find_one_by_uuid_or_name(
            self._list_external_squads(),
            str(discovered.get('external_squad_uuid') or ''),
            _api_name(config.bedolaga.external_squad_name),
            'External Squad',
        )
        node = self._find_one_by_uuid_or_name(
            self._list_nodes(),
            str(discovered.get('remnawave_node_uuid') or ''),
            config.display.internal_name,
            'RemnaWave Node',
        )
        service_user = (
            self._find_service_user_by_uuid_or_config(config, str(discovered.get('transit_service_user_uuid') or ''))
            if delete_transit_service_user and config.transit.service_user
            else None
        )

        if service_user is not None:
            actions.append(self._delete_resource('transit_service_user', service_user, '/api/users/{uuid}', dry_run=dry_run))
        if internal is not None:
            actions.append(self._delete_resource('internal_squad_users', internal, '/api/internal-squads/{uuid}/bulk-actions/remove-users', dry_run=dry_run, missing_ok=True))
        if external is not None:
            actions.append(self._delete_resource('external_squad_users', external, '/api/external-squads/{uuid}/bulk-actions/remove-users', dry_run=dry_run, missing_ok=True))
        if host is not None:
            actions.append(self._delete_resource('host', host, '/api/hosts/{uuid}', dry_run=dry_run))
        if internal is not None:
            actions.append(self._delete_resource('internal_squad', internal, '/api/internal-squads/{uuid}', dry_run=dry_run))
        if external is not None:
            actions.append(self._delete_resource('external_squad', external, '/api/external-squads/{uuid}', dry_run=dry_run))
        if node is not None:
            actions.append(self._delete_resource('node', node, '/api/nodes/{uuid}', dry_run=dry_run, fallback_disable_endpoint='/api/nodes/{uuid}/actions/disable'))
        if profile is not None:
            actions.append(self._delete_resource('config_profile', profile, '/api/config-profiles/{uuid}', dry_run=dry_run))
        if not actions:
            actions.append(RemnaWaveDeleteAction('remnawave', config.display.internal_name, 'missing'))
        return RemnaWaveDecommissionResult(actions=tuple(actions))

    def _delete_resource(
        self,
        kind: str,
        record: dict[str, Any],
        endpoint_template: str,
        *,
        dry_run: bool,
        missing_ok: bool = False,
        fallback_disable_endpoint: str | None = None,
    ) -> RemnaWaveDeleteAction:
        uuid = _required_uuid(record, kind)
        if dry_run:
            return RemnaWaveDeleteAction(kind, uuid, 'would_delete')
        endpoint = endpoint_template.format(uuid=uuid)
        try:
            payload = self.client.delete(endpoint)
        except JsonHttpError as exc:
            if exc.status_code == 404 and missing_ok:
                return RemnaWaveDeleteAction(kind, uuid, 'missing')
            if fallback_disable_endpoint and exc.status_code in {404, 405}:
                self.client.post(fallback_disable_endpoint.format(uuid=uuid))
                return RemnaWaveDeleteAction(kind, uuid, 'disabled_fallback', str(exc))
            raise RemnaWaveAdapterError(f'{kind} delete failed: {exc}') from exc
        status = 'deleted'
        response = payload.get('response') if isinstance(payload, dict) else None
        if isinstance(response, dict) and response.get('isDeleted') is False:
            status = 'delete_requested'
        return RemnaWaveDeleteAction(kind, uuid, status)

    def _find_config_profile(
        self,
        config: NodeConfig,
        *,
        discovered_uuid: str | None,
        expected_name: str,
    ) -> dict[str, Any] | None:
        if discovered_uuid:
            try:
                return _response_object(self.client.get(f'/api/config-profiles/{discovered_uuid}'), 'config profile')
            except JsonHttpError as exc:
                if exc.status_code != 404:
                    raise
        if config.xray.config_profile_uuid:
            try:
                return _response_object(self.client.get(f'/api/config-profiles/{config.xray.config_profile_uuid}'), 'config profile')
            except JsonHttpError as exc:
                if exc.status_code != 404:
                    raise
        return self._find_one_by_name(self._list_config_profiles(), expected_name, 'Config Profile')

    def _get_profile(self, config: NodeConfig) -> dict[str, Any]:
        cached = self._profile_cache.get(config.display.internal_name)
        if cached is not None:
            return cached
        rendered = render_xray_config_profile(config, secret_store=self.secret_store, ensure_missing=False)
        profile = self._find_config_profile(config, discovered_uuid=None, expected_name=rendered.name)
        if profile is None:
            raise RemnaWaveAdapterError('config profile is missing; run pre-bootstrap first')
        self._profile_cache[config.display.internal_name] = profile
        return profile

    def _get_node(self, config: NodeConfig) -> dict[str, Any]:
        cached = self._node_cache.get(config.display.internal_name)
        if cached is not None:
            return cached
        node = self._find_one_by_name(self._list_nodes(), config.display.internal_name, 'RemnaWave Node')
        if node is None:
            raise RemnaWaveAdapterError('RemnaWave Node is missing; run pre-bootstrap first')
        self._node_cache[config.display.internal_name] = node
        return node

    def _get_or_create_node_secret(self, config: NodeConfig) -> str:
        check = self.secret_store.check_ref(config.remnanode.secret_key_ref)
        if check.exists and check.readable:
            return self.secret_store.read_text(config.remnanode.secret_key_ref)
        return fetch_remnawave_node_secret_key(
            api_url=self.api_url,
            auth=self.auth,
            timeout_seconds=self.client.timeout_seconds,
            verify_tls=self.client.verify_tls,
        )

    def _find_host_profile_inbound(self, config: NodeConfig, profile: dict[str, Any]) -> dict[str, Any]:
        if config.host.inbound_ref == 'transit':
            if not config.transit.inbound_tag:
                raise RemnaWaveAdapterError('host.inbound_ref=transit requires transit.inbound_tag')
            return self._find_profile_inbound(profile, config.transit.inbound_tag)
        return self._find_public_profile_inbound(config, profile)

    def _find_public_profile_inbound(self, config: NodeConfig, profile: dict[str, Any]) -> dict[str, Any]:
        expected_tag = public_inbound_tag(config)
        try:
            return self._find_profile_inbound(profile, expected_tag)
        except RemnaWaveAdapterError as primary_error:
            if expected_tag == PUBLIC_INBOUND_TAG:
                raise
            try:
                return self._find_profile_inbound(profile, PUBLIC_INBOUND_TAG)
            except RemnaWaveAdapterError:
                raise primary_error

    def _find_profile_inbound(self, profile: dict[str, Any], tag: str) -> dict[str, Any]:
        matches = [inbound for inbound in _profile_inbounds(profile) if inbound.get('tag') == tag]
        if len(matches) != 1:
            raise RemnaWaveAdapterError(f'expected exactly one profile inbound with tag {tag!r}, found {len(matches)}')
        return matches[0]

    def _find_host_by_uuid_or_config(self, config: NodeConfig, uuid: str) -> dict[str, Any] | None:
        normalized_uuid = uuid.strip()
        if normalized_uuid:
            hosts = self._list_hosts()
            matches = [host for host in hosts if host.get('uuid') == normalized_uuid]
            if len(matches) > 1:
                raise RemnaWaveAdapterError(
                    f'found {len(matches)} Hosts with uuid {normalized_uuid!r}; manual cleanup required',
                )
            if matches:
                return matches[0]
        return self._find_host(config)


    def _find_host(self, config: NodeConfig) -> dict[str, Any] | None:
        hosts = self._list_hosts()
        remark = _truncate_api_text(config.effective_host_remark(), 40)
        matches = [
            host for host in hosts
            if host.get('remark') == remark
            and host.get('address') == config.effective_host_address()
        ]
        if len(matches) > 1:
            raise RemnaWaveAdapterError(f'found {len(matches)} Hosts for {config.effective_host_remark()!r}; manual cleanup required')
        if matches:
            return matches[0]
        remark_matches = [host for host in hosts if host.get('remark') == remark]
        if len(remark_matches) > 1:
            raise RemnaWaveAdapterError(f'found {len(remark_matches)} Hosts with remark {remark!r}; manual cleanup required')
        return remark_matches[0] if remark_matches else None

    def _find_service_user_by_uuid_or_config(self, config: NodeConfig, uuid: str) -> dict[str, Any] | None:
        normalized_uuid = uuid.strip()
        if normalized_uuid:
            users = self._list_users()
            matches = [user for user in users if user.get('uuid') == normalized_uuid]
            if len(matches) > 1:
                raise RemnaWaveAdapterError(
                    f'found {len(matches)} transit service users with uuid {normalized_uuid!r}; manual cleanup required',
                )
            if matches:
                return matches[0]
        return self._find_service_user(config)


    def _find_service_user(self, config: NodeConfig) -> dict[str, Any] | None:
        users = self._list_users()
        matches = [
            user for user in users
            if user.get('username') == config.transit.service_user and user.get('tag') == 'SYSTEM_TRANSIT'
        ]
        if len(matches) > 1:
            raise RemnaWaveAdapterError(
                f'found {len(matches)} transit service users named {config.transit.service_user!r}; manual cleanup required',
            )
        return matches[0] if matches else None

    def _sync_existing_service_user_secret(self, config: NodeConfig, user: dict[str, Any]) -> None:
        credential_ref = config.transit.service_user_credential_ref
        if not credential_ref:
            return
        panel_uuid = str(user.get('vlessUuid') or '').strip()
        if not panel_uuid:
            raise RemnaWaveAdapterError('existing transit service user has no vlessUuid')
        check = self.secret_store.check_ref(credential_ref)
        if check.exists:
            local_uuid = self.secret_store.read_text(credential_ref)
            if local_uuid != panel_uuid:
                raise RemnaWaveAdapterError('local transit user UUID differs from existing RemnaWave user')
            return
        self.secret_store.write_text(credential_ref, panel_uuid, overwrite=False)

    def _list_config_profiles(self) -> list[dict[str, Any]]:
        payload = self.client.get('/api/config-profiles')
        response = _response_object(payload, 'config profiles')
        return _dict_list(response.get('configProfiles'))

    def _list_nodes(self) -> list[dict[str, Any]]:
        return _response_list(self.client.get('/api/nodes'), 'nodes')

    def _list_hosts(self) -> list[dict[str, Any]]:
        return _response_list(self.client.get('/api/hosts'), 'hosts')

    def _list_internal_squads(self) -> list[dict[str, Any]]:
        response = _response_object(self.client.get('/api/internal-squads'), 'internal squads')
        return _dict_list(response.get('internalSquads'))

    def _list_external_squads(self) -> list[dict[str, Any]]:
        response = _response_object(self.client.get('/api/external-squads'), 'external squads')
        return _dict_list(response.get('externalSquads'))

    def _list_users(self) -> list[dict[str, Any]]:
        response = _response_object(self.client.get('/api/users', params={'size': REMNAWAVE_USERS_PAGE_SIZE}), 'users')
        return _dict_list(response.get('users'))

    @staticmethod
    def _find_one_by_name(items: list[dict[str, Any]], name: str, label: str) -> dict[str, Any] | None:
        matches = [item for item in items if item.get('name') == name]
        if len(matches) > 1:
            raise RemnaWaveAdapterError(f'found {len(matches)} {label} objects named {name!r}; manual cleanup required')
        return matches[0] if matches else None

    @staticmethod
    def _find_one_by_uuid_or_name(
        items: list[dict[str, Any]],
        uuid: str,
        name: str,
        label: str,
    ) -> dict[str, Any] | None:
        normalized_uuid = uuid.strip()
        if normalized_uuid:
            uuid_matches = [item for item in items if item.get('uuid') == normalized_uuid]
            if len(uuid_matches) > 1:
                raise RemnaWaveAdapterError(
                    f'found {len(uuid_matches)} {label} objects with uuid {normalized_uuid!r}; manual cleanup required',
                )
            if uuid_matches:
                return uuid_matches[0]
        return HttpRemnaWaveAdapter._find_one_by_name(items, name, label)


def _local_delete(mapping: dict[str, Any], key: str, kind: str, *, dry_run: bool) -> RemnaWaveDeleteAction:
    if key not in mapping:
        return RemnaWaveDeleteAction(kind, key, 'missing')
    if dry_run:
        return RemnaWaveDeleteAction(kind, key, 'would_delete')
    del mapping[key]
    return RemnaWaveDeleteAction(kind, key, 'deleted')


def _new_secret_key() -> str:
    return f'local-rw-node-{secrets.token_urlsafe(32)}'


class DiscoveredRemnaWaveAdapter:
    """State-backed adapter for Bedolaga-only replays after RemnaWave is done.

    This is useful when the operator has already created/verified RemnaWave
    objects and only needs to attach the discovered squad UUIDs to Bedolaga.
    """

    def __init__(self, discovered: dict[str, object]):
        self.discovered = discovered

    def ensure_config_profile(self, config: NodeConfig, discovered_uuid: str | None = None) -> ConfigProfileRecord:
        uuid = str(self.discovered.get('config_profile_uuid') or discovered_uuid or config.xray.config_profile_uuid or '')
        if not uuid:
            raise RemnaWaveAdapterError('missing discovered config_profile_uuid')
        return ConfigProfileRecord(uuid=uuid, status='discovered')

    def ensure_node(self, config: NodeConfig) -> RemnaWaveNodeRecord:
        uuid = self._required('remnawave_node_uuid')
        return RemnaWaveNodeRecord(uuid=uuid, secret_key='<discovered-not-read>', status='discovered')

    def ensure_node_online(self, config: NodeConfig) -> None:
        return None

    def ensure_host(self, config: NodeConfig) -> HostRecord:
        return HostRecord(uuid=self._required('host_uuid'), status='discovered')

    def ensure_internal_squad(self, config: NodeConfig, *, host_uuid: str) -> InternalSquadRecord:
        return InternalSquadRecord(uuid=self._required('internal_squad_uuid'), status='discovered')

    def ensure_external_squad(self, config: NodeConfig, *, internal_squad_uuid: str) -> ExternalSquadRecord:
        return ExternalSquadRecord(uuid=self._required('external_squad_uuid'), status='discovered')

    def ensure_transit_service_user(self, config: NodeConfig, *, internal_squad_uuid: str | None = None) -> ServiceUserRecord | None:
        uuid = self.discovered.get('transit_service_user_uuid')
        if not uuid:
            return None
        return ServiceUserRecord(uuid=str(uuid), status='discovered')

    def ensure_profile_update(
        self,
        config: NodeConfig,
        *,
        profile_uuid: str,
        route_overrides: RouteOverrides | None = None,
    ) -> ProfileUpdateRecord:
        key = str(self.discovered.get('profile_update_key') or f'{profile_uuid}:{config.display.internal_name}')
        return ProfileUpdateRecord(key=key, status='discovered')

    def _required(self, key: str) -> str:
        value = self.discovered.get(key)
        if not value:
            raise RemnaWaveAdapterError(f'missing discovered {key}; complete RemnaWave phase first')
        return str(value)


def _response_object(payload: dict[str, Any], label: str) -> dict[str, Any]:
    response = payload.get('response')
    if not isinstance(response, dict):
        raise RemnaWaveAdapterError(f'RemnaWave {label} response is not an object')
    return response


def _response_list(payload: dict[str, Any], label: str) -> list[dict[str, Any]]:
    response = payload.get('response')
    if isinstance(response, list):
        return _dict_list(response)
    if isinstance(response, dict):
        for key in (label, f'{label}List'):
            if key in response:
                return _dict_list(response.get(key))
    raise RemnaWaveAdapterError(f'RemnaWave {label} response is not a list')


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _required_uuid(record: dict[str, Any], label: str) -> str:
    value = record.get('uuid')
    if not isinstance(value, str) or not value:
        raise RemnaWaveAdapterError(f'{label} response has no uuid')
    return value


def _profile_inbounds(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return _dict_list(profile.get('inbounds'))


def _api_name(name: str) -> str:
    cleaned = ''.join(ch if ch.isalnum() or ch in {'_', '-', ' '} else '-' for ch in name)
    cleaned = ' '.join(cleaned.split()).strip()
    if len(cleaned) < 2:
        cleaned = f'{cleaned}xx'
    return _truncate(cleaned, 30)


def _profile_name(config: NodeConfig) -> str:
    raw = f'tpl {config.display.internal_name}'
    cleaned = ''.join(ch if ch.isalnum() or ch in {'_', '-', ' '} else '-' for ch in raw)
    cleaned = ' '.join(cleaned.split())
    if len(cleaned) <= 30:
        return cleaned
    suffix = hashlib.sha256(config.display.internal_name.encode('utf-8')).hexdigest()[:6]
    return f'{cleaned[:23].rstrip()} {suffix}'


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _truncate_api_text(value: str, limit: int) -> str:
    stripped = value.strip()
    if _utf16_units(stripped) <= limit:
        return stripped
    chars: list[str] = []
    used = 0
    for char in stripped:
        units = _utf16_units(char)
        if used + units > limit:
            break
        chars.append(char)
        used += units
    return ''.join(chars).strip()


def _host_sni(config: NodeConfig) -> str:
    return config.effective_reality_server_names()[0]


def _truncate_host_description(value: str) -> str:
    limit = 30
    truncated = _truncate_api_text(value, limit)
    for opener, closer in (('[', ']'), ('(', ')'), ('{', '}')):
        if truncated.count(opener) <= truncated.count(closer):
            continue
        closer_units = _utf16_units(closer)
        base = _truncate_api_text(truncated, limit - closer_units).rstrip(' -_,.;:/')
        if base.endswith(opener):
            return base[:-1].rstrip() or _truncate_api_text(value, limit)
        return f'{base}{closer}' if base else _truncate_api_text(value, limit)
    return truncated


def _utf16_units(value: str) -> int:
    return len(value.encode('utf-16-le')) // 2


def _merged_squad_uuids(user: dict[str, Any], internal_squad_uuid: str | None) -> list[str]:
    values: list[str] = []
    for item in _dict_list(user.get('activeInternalSquads')):
        uuid = item.get('uuid')
        if isinstance(uuid, str) and uuid:
            values.append(uuid)
    if internal_squad_uuid:
        values.append(internal_squad_uuid)
    return sorted(set(values))
