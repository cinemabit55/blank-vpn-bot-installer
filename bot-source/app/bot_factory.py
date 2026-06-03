"""Factory for creating Bot instances with proxy and custom API server support."""

import asyncio

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError

from app.config import settings


logger = structlog.get_logger(__name__)

_FAST_FAIL_METHODS = {
    'AnswerCallbackQuery',
    'EditMessageCaption',
    'EditMessageMedia',
    'EditMessageReplyMarkup',
    'EditMessageText',
    'SendPhoto',
}


class TelegramNetworkRetryMiddleware:
    """Retry transient Telegram API transport failures once or twice."""

    def __init__(self, attempts: int = 3, base_delay: float = 0.35):
        self.attempts = attempts
        self.base_delay = base_delay

    async def __call__(self, make_request, bot, method):
        last_error = None
        method_name = type(method).__name__
        attempts = 1 if method_name in _FAST_FAIL_METHODS else self.attempts
        for attempt in range(1, attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                logger.warning(
                    'Telegram API network error, retrying',
                    method=type(method).__name__,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(exc),
                )
                await asyncio.sleep(self.base_delay * attempt)
        raise last_error


def create_bot(token: str | None = None, **kwargs) -> Bot:
    """Create a Bot instance with SOCKS5 proxy and/or custom API server."""
    proxy_url = settings.get_proxy_url()
    telegram_api_url = settings.get_telegram_api_url()

    from aiogram.client.telegram import TelegramAPIServer

    session_kwargs: dict = {}
    if proxy_url:
        session_kwargs['proxy'] = proxy_url
    if telegram_api_url:
        session_kwargs['api'] = TelegramAPIServer.from_base(telegram_api_url)

    session = AiohttpSession(**session_kwargs)
    session.middleware.register(TelegramNetworkRetryMiddleware())

    kwargs.setdefault('default', DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)
