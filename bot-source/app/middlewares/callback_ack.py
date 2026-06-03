from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, TelegramObject

from app.utils.callback_render_guard import register_callback_render


logger = structlog.get_logger(__name__)

AUTO_ACK_EXACT = {
    'back_to_menu',
    'current_page',
    'noop',
    'menu_subscription',
    'menu_info',
    'menu_resources',
    'menu_apps',
    'menu_promocode',
    'menu_referrals',
    'menu_support',
    'menu_balance',
    'menu_trial',
    'menu_server_status',
    'admin_panel',
    'moderator_panel',
    'menu_buy',
    'subscription_upgrade',
    'subscription_purchase',
}

AUTO_ACK_PREFIXES = (
    'menu_faq',
    'menu_info_',
    'menu_privacy',
    'menu_public_offer',
    'apps_select_',
    'apps_dl_',
    'sm:',
)


def should_auto_ack_callback(callback_data: str | None) -> bool:
    if not callback_data:
        return False
    if callback_data in AUTO_ACK_EXACT:
        return True
    return callback_data.startswith(AUTO_ACK_PREFIXES)


class CallbackAckMiddleware(BaseMiddleware):
    """Answer safe navigation callbacks before rendering slow screens.

    Telegram keeps the pressed button spinning until answerCallbackQuery is sent.
    Heavy DB/API work or message edits can take a couple of seconds, so for
    simple navigation callbacks we acknowledge first and let rendering continue.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            data['callback_render_token'] = register_callback_render(event)

        if isinstance(event, CallbackQuery) and should_auto_ack_callback(event.data):
            try:
                await event.answer()
                data['callback_auto_answered'] = True
            except TelegramAPIError as exc:
                logger.debug('Не удалось заранее ответить на callback', event_data=event.data, error=str(exc))

        return await handler(event, data)
