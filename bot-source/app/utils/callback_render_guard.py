"""Small in-process guard against stale callback screen renders."""

from __future__ import annotations

from time import monotonic

from aiogram.types import CallbackQuery

_TOKEN_TTL_SECONDS = 180.0
_latest_token_by_user: dict[int, tuple[str, float]] = {}
_token_by_callback_id: dict[str, tuple[int, str, float]] = {}
_last_cleanup = 0.0


def _callback_user_id(callback: CallbackQuery) -> int | None:
    user = getattr(callback, "from_user", None)
    return getattr(user, "id", None)


def _cleanup(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < 60.0:
        return
    _last_cleanup = now
    cutoff = now - _TOKEN_TTL_SECONDS
    stale_callback_ids = [
        callback_id
        for callback_id, (_, _, created_at) in _token_by_callback_id.items()
        if created_at < cutoff
    ]
    for callback_id in stale_callback_ids:
        _token_by_callback_id.pop(callback_id, None)

    stale_users = [
        user_id
        for user_id, (_, created_at) in _latest_token_by_user.items()
        if created_at < cutoff
    ]
    for user_id in stale_users:
        _latest_token_by_user.pop(user_id, None)


def register_callback_render(callback: CallbackQuery) -> str | None:
    """Mark callback as the latest UI-rendering action for this user."""
    user_id = _callback_user_id(callback)
    callback_id = getattr(callback, "id", None)
    if user_id is None or not callback_id:
        return None

    now = monotonic()
    _cleanup(now)
    token = f"{callback_id}:{now:.6f}"
    _latest_token_by_user[user_id] = (token, now)
    _token_by_callback_id[callback_id] = (user_id, token, now)
    return token


def is_latest_callback_render(callback: CallbackQuery) -> bool:
    """Return False when a newer callback from this user already started rendering."""
    callback_id = getattr(callback, "id", None)
    if not callback_id:
        return True

    item = _token_by_callback_id.get(callback_id)
    if item is None:
        return True

    user_id, token, _ = item
    latest = _latest_token_by_user.get(user_id)
    return latest is not None and latest[0] == token
