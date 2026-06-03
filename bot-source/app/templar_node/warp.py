"""Cloudflare WARP registration helper for Xray WireGuard outbound."""

from __future__ import annotations

import base64
import json
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.templar_node.credentials import (
    CredentialError,
    compute_warp_reserved_from_client_id,
    read_warp_registration,
)
from app.templar_node.schemas import NodeConfig, WarpMode
from app.templar_node.secrets import LocalSecretStore


WARP_API_BASE_URL = 'https://api.cloudflareclient.com'
WARP_API_VERSION = 'v0a2483'
WARP_CLIENT_VERSION = 'a-6.81-2410012252.0'
WARP_USER_AGENT = '1.1.1.1/6.81'
WARP_DEFAULT_ENDPOINT_PORT = 2408


class WarpRegistrationError(RuntimeError):
    """Raised when WARP registration cannot finish safely."""


@dataclass(frozen=True)
class WarpRegisterResult:
    ref: str
    path: Path
    status: str
    device_id: str | None
    endpoint: str
    address: tuple[str, ...]
    reserved: tuple[int, int, int]

    def to_lines(self) -> list[str]:
        return [
            f'WARP registration: {self.ref} ({self.status})',
            f'Path: {self.path}',
            f'Device ID: {self.device_id or "<existing>"}',
            f'Endpoint: {self.endpoint}',
            f'Address: {", ".join(self.address)}',
            f'Reserved: [{self.reserved[0]}, {self.reserved[1]}, {self.reserved[2]}]',
        ]


@dataclass(frozen=True)
class WarpRegistrationOptions:
    api_base_url: str = WARP_API_BASE_URL
    api_version: str = WARP_API_VERSION
    client_version: str = WARP_CLIENT_VERSION
    user_agent: str = WARP_USER_AGENT
    device_model: str = 'Templar Node'
    license_key: str | None = None
    timeout_seconds: int = 20
    verify_tls: bool = True


class WarpApiClient:
    """Small stdlib client for Cloudflare WARP registration API."""

    def __init__(self, options: WarpRegistrationOptions):
        self.options = options

    def register(self, *, public_key: str) -> dict[str, Any]:
        body = {
            'fcm_token': '',
            'install_id': '',
            'key': public_key,
            'locale': 'en_US',
            'model': self.options.device_model,
            'tos': _utc_now_rfc3339(),
            'type': 'Android',
        }
        return self._request('POST', f'/{self.options.api_version}/reg', json_body=body)

    def fetch_config(self, *, device_id: str, access_token: str) -> dict[str, Any]:
        return self._request('GET', f'/{self.options.api_version}/reg/{device_id}', access_token=access_token)

    def update_license(self, *, device_id: str, access_token: str, license_key: str) -> None:
        self._request(
            'PUT',
            f'/{self.options.api_version}/reg/{device_id}/account',
            access_token=access_token,
            json_body={'license': license_key},
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        access_token: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f'{self.options.api_base_url.rstrip("/")}/{endpoint.lstrip("/")}'
        headers = {
            'Accept': 'application/json; charset=UTF-8',
            'Content-Type': 'application/json; charset=UTF-8',
            'CF-Client-Version': self.options.client_version,
            'User-Agent': self.options.user_agent,
        }
        if access_token:
            headers['Authorization'] = f'Bearer {access_token}'
        data = json.dumps(json_body).encode('utf-8') if json_body is not None else None
        request = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(
                request,
                timeout=self.options.timeout_seconds,
                context=_tls12_context(verify_tls=self.options.verify_tls),
            ) as response:
                raw = response.read().decode('utf-8')
                return _decode_json(raw, url)
        except HTTPError as exc:
            raw = exc.read().decode('utf-8', errors='replace')
            raise WarpRegistrationError(f'WARP API HTTP {exc.code}: {_error_message(raw)}') from exc
        except (URLError, TimeoutError) as exc:
            raise WarpRegistrationError(f'cannot connect to WARP API {url}: {exc}') from exc


def ensure_warp_registration_for_config(
    config: NodeConfig,
    *,
    secret_store: LocalSecretStore,
    options: WarpRegistrationOptions | None = None,
    overwrite: bool = False,
    api_client: WarpApiClient | None = None,
) -> WarpRegisterResult | None:
    """Ensure ``config.warp.registration_ref`` exists for Xray-native WARP."""
    if config.warp.mode != WarpMode.XRAY_NATIVE:
        return None
    if not config.warp.registration_ref:
        raise WarpRegistrationError('xray_native WARP requires warp.registration_ref')

    ref = config.warp.registration_ref
    check = secret_store.check_ref(ref)
    if check.exists and not overwrite:
        try:
            existing = read_warp_registration(secret_store, ref)
        except CredentialError as exc:
            raise WarpRegistrationError(str(exc)) from exc
        return WarpRegisterResult(
            ref=ref,
            path=secret_store.path_for_ref(ref),
            status='existing',
            device_id=None,
            endpoint=existing.endpoint,
            address=existing.address,
            reserved=existing.reserved,
        )

    options = options or WarpRegistrationOptions()
    client = api_client or WarpApiClient(options)
    private_key, public_key = _generate_wireguard_keypair()
    registration_payload = client.register(public_key=public_key)
    registration = _response_object(registration_payload)
    device_id = _required_string(registration, 'id')
    access_token = _required_string(registration, 'token')
    if options.license_key:
        client.update_license(device_id=device_id, access_token=access_token, license_key=options.license_key)
    config_payload = client.fetch_config(device_id=device_id, access_token=access_token)
    fetched = _response_object(config_payload)
    secret_payload = build_warp_secret_payload(
        registration=registration,
        fetched=fetched,
        private_key=private_key,
        public_key=public_key,
    )
    path = secret_store.write_text(ref, json.dumps(secret_payload, ensure_ascii=False, sort_keys=True), overwrite=overwrite)
    stored = read_warp_registration(secret_store, ref)
    return WarpRegisterResult(
        ref=ref,
        path=path,
        status='created' if not check.exists else 'updated',
        device_id=device_id,
        endpoint=stored.endpoint,
        address=stored.address,
        reserved=stored.reserved,
    )


def build_warp_secret_payload(
    *,
    registration: dict[str, Any],
    fetched: dict[str, Any],
    private_key: str,
    public_key: str,
) -> dict[str, Any]:
    """Build the JSON secret consumed by ``read_warp_registration``."""
    config = _config_object(fetched) or _config_object(registration)
    if not config:
        raise WarpRegistrationError('WARP API response has no config object')
    interface = _dict_value(config, 'interface')
    addresses = _dict_value(interface, 'addresses')
    peers = config.get('peers')
    if not isinstance(peers, list) or not peers or not isinstance(peers[0], dict):
        raise WarpRegistrationError('WARP API response config has no peer')
    peer = peers[0]
    client_id = _client_id_from_config(config)
    try:
        reserved = compute_warp_reserved_from_client_id(client_id)
    except CredentialError as exc:
        raise WarpRegistrationError(str(exc)) from exc

    account = _dict_value(fetched, 'account', required=False) or _dict_value(registration, 'account', required=False)
    endpoint = _endpoint_from_peer(peer)
    return {
        'source': 'cloudflare_warp_consumer_api',
        'deviceId': _required_string(registration, 'id'),
        'accessToken': _required_string(registration, 'token'),
        'license': account.get('license') if account else None,
        'warpPlus': bool(account.get('warp_plus')) if account else False,
        'privateKey': private_key,
        'publicKey': public_key,
        'secretKey': private_key,
        'address': [_required_string(addresses, 'v4'), _required_string(addresses, 'v6')],
        'peerPublicKey': _required_string(peer, 'public_key'),
        'endpoint': endpoint,
        'clientId': client_id,
        'reserved': [reserved[0], reserved[1], reserved[2]],
        'mtu': 1280,
        'keepAlive': 25,
        'createdAt': _utc_now_rfc3339(),
    }


def _generate_wireguard_keypair() -> tuple[str, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import x25519
    except Exception as exc:
        raise WarpRegistrationError(
            'cryptography package is required to generate WARP WireGuard keys; install project requirements',
        ) from exc
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (_standard_b64(private_bytes), _standard_b64(public_bytes))


def _endpoint_from_peer(peer: dict[str, Any]) -> str:
    endpoint = _dict_value(peer, 'endpoint')
    host = _required_string(endpoint, 'host')
    endpoint_v4 = endpoint.get('v4')
    port = _port_from_endpoint(host) or _port_from_endpoint(str(endpoint_v4 or '')) or WARP_DEFAULT_ENDPOINT_PORT
    if isinstance(endpoint_v4, str) and endpoint_v4:
        endpoint_ip = endpoint_v4.rsplit(':', 1)[0] if ':' in endpoint_v4 else endpoint_v4
        return f'{endpoint_ip}:{port}'
    return host if ':' in host else f'{host}:{port}'


def _client_id_from_config(config: dict[str, Any]) -> str:
    value = config.get('client_id') or config.get('clientId')
    if not isinstance(value, str) or not value.strip():
        raise WarpRegistrationError('WARP API config has no client_id for reserved')
    return value.strip()


def _config_object(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = payload.get('config')
    return value if isinstance(value, dict) else None


def _response_object(payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get('response')
    if isinstance(response, dict):
        return response
    return payload


def _dict_value(payload: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    if required:
        raise WarpRegistrationError(f'WARP API response missing object {key!r}')
    return {}


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WarpRegistrationError(f'WARP API response missing string {key!r}')
    return value.strip()


def _port_from_endpoint(value: str) -> int | None:
    if ':' not in value:
        return None
    maybe_port = value.rsplit(':', 1)[1]
    return int(maybe_port) if maybe_port.isdigit() else None


def _decode_json(body: str, url: str) -> dict[str, Any]:
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise WarpRegistrationError(f'non-JSON response from {url}: {exc}') from exc
    if not isinstance(payload, dict):
        raise WarpRegistrationError(f'JSON response from {url} must be an object')
    return payload


def _error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:500]
    if isinstance(payload, dict):
        message = payload.get('message') or payload.get('error') or payload.get('errors')
        if message:
            return str(message)
    return body[:500]


def _tls12_context(*, verify_tls: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _standard_b64(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def _utc_now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
