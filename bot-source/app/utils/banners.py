"""Templar VPN: точечные баннеры для разделов бота.

Не использует ENABLE_LOGO_MODE и не влияет на остальные экраны (платежи,
админка, FAQ и т.д.) — там продолжает работать стандартный edit_or_answer_photo.

Использование:
    from app.utils.banners import show_with_banner
    await show_with_banner(callback, "profile", caption, keyboard)
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import structlog
from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import FSInputFile, InaccessibleMessage, InputMediaPhoto

from app.utils.callback_render_guard import is_latest_callback_render
from app.utils.message_patch import caption_exceeds_telegram_limit

logger = structlog.get_logger(__name__)

BANNERS_DIR = Path(__file__).resolve().parent.parent / "assets" / "banners"

BANNER_FALLBACK_LANGUAGE = "ru"

BANNERS = {
    "main_menu": {"ru": "main_menu_ru.jpg", "en": "main_menu_en.jpg", "fallback": "main_menu.jpg"},
    "profile": {"ru": "profile_ru.jpg", "en": "profile_en.jpg", "fallback": "profile.jpg"},
    "referral": {"ru": "referral_ru.jpg", "en": "referral_en.jpg", "fallback": "referral.jpg"},
    "support": {"ru": "support_ru.jpg", "en": "support_en.jpg", "fallback": "support.jpg"},
    "download": {"ru": "download_ru.jpg", "en": "download_en.jpg", "fallback": "download.jpg"},
    "about": {"ru": "about_ru.jpg", "en": "about_en.jpg", "fallback": "about.jpg"},
    "resources": {"ru": "resources_ru.jpg", "en": "resources_en.jpg", "fallback": "resources.jpg"},
    "welcome": {"ru": "welcome.jpg", "en": "welcome.jpg", "fallback": "welcome.jpg"},
}

BANNER_FILE_ID_CACHE_PATH = Path(os.getenv("BANNER_FILE_ID_CACHE_PATH", "/app/data/banner_file_ids.json"))


def _load_file_id_cache() -> dict[str, str]:
    try:
        if not BANNER_FILE_ID_CACHE_PATH.exists():
            return {}
        raw = json.loads(BANNER_FILE_ID_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value}
    except Exception as exc:
        logger.warning("Failed to load banner file_id cache", error=str(exc))
        return {}


def _save_file_id_cache() -> None:
    try:
        BANNER_FILE_ID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_file_id_cache()
        merged = {**existing, **_FILE_ID_CACHE}
        _FILE_ID_CACHE.clear()
        _FILE_ID_CACHE.update(merged)
        tmp_path = BANNER_FILE_ID_CACHE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(BANNER_FILE_ID_CACHE_PATH)
    except Exception as exc:
        logger.debug("Failed to save banner file_id cache", error=str(exc))


# Кэш file_id, чтобы не загружать баннеры заново после каждого рестарта.
_FILE_ID_CACHE: dict[str, str] = _load_file_id_cache()

BANNER_MEDIA_MAX_RETRIES = 1
BANNER_MEDIA_RETRY_DELAY = 0.25
BANNER_MEDIA_TIMEOUT = 4.0
# Fast production mode: Telegram media edits/uploads can stall callback handling.
# Keep banner assets available, but render banner screens as text for responsiveness.
BANNER_MEDIA_ENABLED = True


async def _answer_photo_with_retries(
    target,
    banner_name: str,
    media,
    caption: str,
    keyboard: Optional[types.InlineKeyboardMarkup],
    parse_mode: str,
) -> types.Message:
    for attempt in range(BANNER_MEDIA_MAX_RETRIES):
        try:
            return await asyncio.wait_for(
                target.answer_photo(
                    photo=media,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                ),
                timeout=BANNER_MEDIA_TIMEOUT,
            )
        except (TelegramNetworkError, TimeoutError) as exc:
            if attempt >= BANNER_MEDIA_MAX_RETRIES - 1:
                raise
            logger.warning(
                "TelegramNetworkError in banner answer_photo, retrying",
                banner=banner_name, attempt=attempt + 1, error=str(exc),
            )
            await asyncio.sleep(BANNER_MEDIA_RETRY_DELAY * (attempt + 1))
    raise RuntimeError("unreachable banner retry state")


def _normalize_banner_language(language: str | None) -> str:
    language_code = (language or BANNER_FALLBACK_LANGUAGE).split("-", 1)[0].strip().lower()
    return "en" if language_code == "en" else BANNER_FALLBACK_LANGUAGE


def _cache_key(name: str, language: str | None) -> str:
    return f"{name}:{_normalize_banner_language(language)}"


def _target_language(target) -> str | None:
    user = getattr(target, "from_user", None)
    return getattr(user, "language_code", None)


def _resolve_banner_path(name: str, language: str | None = None) -> Optional[Path]:
    variants = BANNERS.get(name)
    if not variants:
        return None

    preferred_language = _normalize_banner_language(language)
    candidates = [
        variants.get(preferred_language),
        variants.get(BANNER_FALLBACK_LANGUAGE),
        variants.get("fallback"),
    ]
    for file_name in candidates:
        if not file_name:
            continue
        path = BANNERS_DIR / file_name
        if path.exists():
            return path
    return None


def get_banner_media(name: str, language: str | None = None):
    """Вернуть file_id (если кэш) либо FSInputFile. None — если файла нет."""
    cache_key = _cache_key(name, language)
    cached = _FILE_ID_CACHE.get(cache_key)
    if cached:
        return cached
    path = _resolve_banner_path(name, language)
    if path is None:
        logger.warning("Banner file not found", banner=name, language=language, dir=str(BANNERS_DIR))
        return None
    return FSInputFile(path)


def cache_banner_file_id(name: str, message: types.Message, language: str | None = None) -> None:
    if not message:
        return
    photo = getattr(message, "photo", None)
    if not photo:
        return
    file_id = photo[-1].file_id
    cache_key = _cache_key(name, language)
    if file_id and _FILE_ID_CACHE.get(cache_key) != file_id:
        _FILE_ID_CACHE[cache_key] = file_id
        _save_file_id_cache()


async def show_with_banner(
    callback: types.CallbackQuery,
    banner_name: str,
    caption: str,
    keyboard: Optional[types.InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    language: str | None = None,
) -> None:
    """Показать сообщение с баннером в ответ на callback.

    - caption длиннее лимита фото (1024) → fallback на текст.
    - banner-файла нет → fallback на текст.
    - callback.message — фото → edit_media (без мерцания).
    - callback.message — текст → удалить и отправить новое фото.
    """
    from app.utils.photo_message import edit_or_answer_photo  # noqa: PLC0415

    effective_language = language or _target_language(callback)

    if not is_latest_callback_render(callback):
        return

    if caption_exceeds_telegram_limit(caption):
        await edit_or_answer_photo(callback, caption, keyboard, parse_mode, force_text=True)
        return

    if not BANNER_MEDIA_ENABLED:
        await edit_or_answer_photo(callback, caption, keyboard, parse_mode, force_text=True)
        return

    media = get_banner_media(banner_name, effective_language)
    if media is None:
        await edit_or_answer_photo(callback, caption, keyboard, parse_mode, force_text=True)
        return

    if isinstance(callback.message, InaccessibleMessage):
        try:
            result = await _answer_photo_with_retries(
                callback.message, banner_name, media, caption, keyboard, parse_mode
            )
            cache_banner_file_id(banner_name, result, effective_language)
        except Exception as exc:
            logger.warning("Не удалось отправить новое фото", error=str(exc))
        return

    async def _fallback_to_text(error: Exception) -> None:
        if not is_latest_callback_render(callback):
            return
        logger.warning(
            "show_with_banner fallback to text",
            banner=banner_name, error=str(error),
        )
        await edit_or_answer_photo(callback, caption, keyboard, parse_mode, force_text=True)

    try:
        if callback.message.photo:
            for attempt in range(BANNER_MEDIA_MAX_RETRIES):
                try:
                    if not is_latest_callback_render(callback):
                        return
                    result = await asyncio.wait_for(
                        callback.message.edit_media(
                            InputMediaPhoto(media=media, caption=caption, parse_mode=parse_mode),
                            reply_markup=keyboard,
                        ),
                        timeout=BANNER_MEDIA_TIMEOUT,
                    )
                    if not is_latest_callback_render(callback):
                        return
                    cache_banner_file_id(banner_name, result, effective_language)
                    return
                except (TelegramNetworkError, TimeoutError) as exc:
                    if attempt < BANNER_MEDIA_MAX_RETRIES - 1:
                        logger.warning(
                            "TelegramNetworkError in show_with_banner edit_media, retrying",
                            banner=banner_name, attempt=attempt + 1, error=str(exc),
                        )
                        await asyncio.sleep(BANNER_MEDIA_RETRY_DELAY * (attempt + 1))
                        continue
                    await _fallback_to_text(exc)
                    return
        if not is_latest_callback_render(callback):
            return
        try:
            await callback.message.delete()
        except Exception:
            pass
        if not is_latest_callback_render(callback):
            return
        result = await _answer_photo_with_retries(
            callback.message, banner_name, media, caption, keyboard, parse_mode
        )
        if not is_latest_callback_render(callback):
            return
        cache_banner_file_id(banner_name, result, effective_language)
    except TelegramForbiddenError:
        logger.debug("User blocked bot")
        return
    except (TelegramNetworkError, TimeoutError) as exc:
        await _fallback_to_text(exc)
    except TelegramBadRequest as exc:
        await _fallback_to_text(exc)


async def answer_with_banner(
    target,
    banner_name: str,
    caption: str,
    keyboard: Optional[types.InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    language: str | None = None,
) -> Optional[types.Message]:
    """Отправить новое сообщение с баннером (без редактирования старого).

    target — это types.Message (есть метод .answer_photo). Используется в /start.
    """
    effective_language = language or _target_language(target)

    if caption_exceeds_telegram_limit(caption):
        if hasattr(target, "answer"):
            return await target.answer(caption, reply_markup=keyboard, parse_mode=parse_mode)
        return None

    if not BANNER_MEDIA_ENABLED:
        if hasattr(target, "answer"):
            return await target.answer(caption, reply_markup=keyboard, parse_mode=parse_mode)
        return None

    media = get_banner_media(banner_name, effective_language)
    if media is None:
        if hasattr(target, "answer"):
            return await target.answer(caption, reply_markup=keyboard, parse_mode=parse_mode)
        return None

    try:
        if hasattr(target, "answer_photo"):
            result = await _answer_photo_with_retries(
                target, banner_name, media, caption, keyboard, parse_mode
            )
            cache_banner_file_id(banner_name, result, effective_language)
            return result
    except TelegramForbiddenError:
        logger.debug("User blocked bot, skip banner answer")
        return None
    except (TelegramBadRequest, TelegramNetworkError, TimeoutError) as exc:
        logger.warning(
            "answer_with_banner fallback to text",
            banner=banner_name, error=str(exc),
        )
        if hasattr(target, "answer"):
            return await target.answer(caption, reply_markup=keyboard, parse_mode=parse_mode)
    return None
