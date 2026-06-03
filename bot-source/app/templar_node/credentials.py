"""Credential helpers for Xray profile rendering.

The helpers keep secret material in the configured secret store and expose only
validated, typed values to the RemnaWave/Xray renderer.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from app.templar_node.secrets import LocalSecretStore, SecretStoreError


class CredentialError(ValueError):
    """Raised when a required credential is missing or malformed."""


@dataclass(frozen=True)
class RealityCredentials:
    private_key: str
    public_key: str
    short_ids: tuple[str, ...]
    server_names: tuple[str, ...]

    def public_subset(self) -> dict[str, Any]:
        return {
            'publicKey': self.public_key,
            'shortIds': list(self.short_ids),
            'serverNames': list(self.server_names),
        }


@dataclass(frozen=True)
class WarpRegistration:
    secret_key: str
    address: tuple[str, ...]
    peer_public_key: str
    endpoint: str
    reserved: tuple[int, int, int]
    mtu: int = 1280
    keep_alive: int = 25


def ensure_reality_credentials(
    secret_store: LocalSecretStore,
    ref: str,
    *,
    server_names: list[str],
) -> RealityCredentials:
    """Read or create REALITY X25519 credentials.

    Existing secrets are preserved. New secrets are written as one JSON object:
    ``privateKey/publicKey/shortIds/serverNames``.
    """
    check = secret_store.check_ref(ref)
    if check.exists:
        return read_reality_credentials(secret_store, ref)
    credentials = _generate_reality_credentials(server_names)
    payload = {
        'privateKey': credentials.private_key,
        'publicKey': credentials.public_key,
        'shortIds': list(credentials.short_ids),
        'serverNames': list(credentials.server_names),
    }
    secret_store.write_text(ref, json.dumps(payload, ensure_ascii=False, sort_keys=True), overwrite=False)
    return credentials


def read_reality_credentials(secret_store: LocalSecretStore, ref: str) -> RealityCredentials:
    try:
        payload = secret_store.read_json(ref)
    except SecretStoreError as exc:
        raise CredentialError(str(exc)) from exc
    private_key = _required_string(payload, 'privateKey')
    public_key = _required_string(payload, 'publicKey')
    short_ids = _required_string_list(payload, 'shortIds')
    server_names = _required_string_list(payload, 'serverNames')
    for short_id in short_ids:
        if len(short_id) > 16 or len(short_id) % 2 != 0 or any(ch not in '0123456789abcdef' for ch in short_id.lower()):
            raise CredentialError(f'REALITY shortId in {ref} must be even-length hex up to 16 chars')
    return RealityCredentials(
        private_key=private_key,
        public_key=public_key,
        short_ids=tuple(short_ids),
        server_names=tuple(server_names),
    )


def read_warp_registration(secret_store: LocalSecretStore, ref: str) -> WarpRegistration:
    try:
        payload = secret_store.read_json(ref)
    except SecretStoreError as exc:
        raise CredentialError(str(exc)) from exc
    reserved = payload.get('reserved')
    if not isinstance(reserved, list) or len(reserved) != 3 or any(not isinstance(item, int) or item < 0 or item > 255 for item in reserved):
        raise CredentialError(f'WARP secret {ref} must contain reserved as exactly three bytes')
    mtu = payload.get('mtu', 1280)
    keep_alive = payload.get('keepAlive', payload.get('keep_alive', 25))
    if not isinstance(mtu, int) or mtu < 576 or mtu > 1500:
        raise CredentialError(f'WARP secret {ref} has invalid mtu')
    if not isinstance(keep_alive, int) or keep_alive < 0 or keep_alive > 120:
        raise CredentialError(f'WARP secret {ref} has invalid keepAlive')
    return WarpRegistration(
        secret_key=_required_string(payload, 'secretKey'),
        address=tuple(_required_string_list(payload, 'address')),
        peer_public_key=_required_string(payload, 'peerPublicKey'),
        endpoint=_required_string(payload, 'endpoint'),
        reserved=(reserved[0], reserved[1], reserved[2]),
        mtu=mtu,
        keep_alive=keep_alive,
    )


def ensure_vless_uuid(secret_store: LocalSecretStore, ref: str) -> str:
    check = secret_store.check_ref(ref)
    if check.exists:
        value = secret_store.read_text(ref)
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise CredentialError(f'service user secret {ref} must contain a VLESS UUID') from exc
    value = str(uuid.uuid4())
    secret_store.write_text(ref, value, overwrite=False)
    return value


def compute_warp_reserved_from_client_id(client_id_b64: str) -> tuple[int, int, int]:
    if not client_id_b64 or _looks_like_uuid(client_id_b64):
        raise CredentialError('WARP client id is missing or is a UUID, not a reserved source')
    padded = client_id_b64 + '=' * (-len(client_id_b64) % 4)
    try:
        raw = base64.b64decode(padded)
    except Exception as exc:
        raise CredentialError('WARP client id is not valid base64') from exc
    if len(raw) < 3:
        raise CredentialError('WARP client id decoded to fewer than 3 bytes')
    return (raw[0], raw[1], raw[2])


def _generate_reality_credentials(server_names: list[str]) -> RealityCredentials:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import x25519
    except Exception as exc:
        raise CredentialError(
            'cryptography package is required to generate REALITY credentials; '
            'install project requirements or pre-create the JSON secret',
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
    return RealityCredentials(
        private_key=_raw_urlsafe_b64(private_bytes),
        public_key=_raw_urlsafe_b64(public_bytes),
        short_ids=(secrets.token_hex(8),),
        server_names=tuple(server_names),
    )


def _raw_urlsafe_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CredentialError(f'secret JSON must contain non-empty string {key!r}')
    return value.strip()


def _required_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise CredentialError(f'secret JSON must contain non-empty string list {key!r}')
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CredentialError(f'secret JSON list {key!r} contains a non-string value')
        normalized.append(item.strip())
    return normalized


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
