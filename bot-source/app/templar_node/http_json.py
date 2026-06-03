"""Small stdlib JSON HTTP helper for onboarding preflight checks."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class JsonHttpError(RuntimeError):
    """Raised when a JSON HTTP request cannot complete safely."""

    def __init__(self, message: str, *, status_code: int | None = None, response_data: Any | None = None):
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(message)


@dataclass(frozen=True)
class JsonHttpClient:
    base_url: str
    headers: dict[str, str]
    timeout_seconds: int = 20
    verify_tls: bool = True

    def get(self, endpoint: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self.request('GET', endpoint, params=params)

    def post(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request('POST', endpoint, params=params, json_body=json_body)

    def patch(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request('PATCH', endpoint, params=params, json_body=json_body)

    def put(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request('PUT', endpoint, params=params, json_body=json_body)

    def delete(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request('DELETE', endpoint, params=params, json_body=json_body)

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f'{self.base_url.rstrip("/")}/{endpoint.lstrip("/")}'
        if params:
            url = f'{url}?{urlencode(params)}'
        data = None
        headers = dict(self.headers)
        if json_body is not None:
            data = json.dumps(json_body).encode('utf-8')
            headers.setdefault('Content-Type', 'application/json')
        request = Request(url, data=data, method=method, headers=headers)
        context = None if self.verify_tls else _insecure_ssl_context()
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=context) as response:
                body = response.read().decode('utf-8')
                return _decode_json(body, url)
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            response_data = _try_decode_json(body)
            message = _extract_error_message(response_data) or f'HTTP {exc.code}'
            raise JsonHttpError(message, status_code=exc.code, response_data=response_data) from exc
        except URLError as exc:
            raise JsonHttpError(f'cannot connect to {url}: {exc.reason}') from exc
        except TimeoutError as exc:
            raise JsonHttpError(f'timed out connecting to {url}') from exc


def _decode_json(body: str, url: str) -> dict[str, Any]:
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise JsonHttpError(f'non-JSON response from {url}: {exc}') from exc
    if not isinstance(payload, dict):
        raise JsonHttpError(f'JSON response from {url} must be an object')
    return payload


def _try_decode_json(body: str) -> Any | None:
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError:
        return {'raw_response': body[:500]}


def _extract_error_message(response_data: Any | None) -> str | None:
    if isinstance(response_data, dict):
        message = response_data.get('message') or response_data.get('error')
        errors = response_data.get('errors')
        details = _format_error_details(errors)
        if details:
            return f'{message}: {details}' if message else details
        if message:
            return str(message)
    return None


def _format_error_details(errors: Any | None) -> str | None:
    if not isinstance(errors, list) or not errors:
        return None
    parts: list[str] = []
    for error in errors[:3]:
        if isinstance(error, dict):
            path = error.get('path')
            label = '.'.join(str(part) for part in path) if isinstance(path, list) else str(path or '').strip()
            message = str(error.get('message') or error).strip()
            parts.append(f'{label}: {message}' if label else message)
        else:
            parts.append(str(error))
    if len(errors) > 3:
        parts.append(f'+{len(errors) - 3} more')
    return '; '.join(parts)


def _insecure_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context
