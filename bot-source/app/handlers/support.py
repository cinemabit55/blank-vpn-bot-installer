import structlog
from aiogram import Dispatcher, F, types

from app.database.models import User
from app.keyboards.inline import get_support_keyboard
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


async def show_support_info(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)
    support_info = texts.t(
        'SUPPORT_INFO_TEMPLAR',
        (
            '🛡 <b>Поддержка</b>\n\n'
            'Что-то не работает или есть вопрос? Напишите нам напрямую — на той стороне живой человек.\n\n'
            'Чтобы решить вопрос за один раз, сразу приложите:\n'
            '• устройство и ОС (iOS / Android / Windows / macOS)\n'
            '• в чём именно проблема\n'
            '• скриншот или текст ошибки\n\n'
            'Нажмите кнопку ниже — откроется чат.'
        ),
    )
    from app.utils.banners import show_with_banner  # noqa: PLC0415
    await show_with_banner(
        callback=callback,
        banner_name='support',
        caption=support_info,
        keyboard=get_support_keyboard(db_user.language),
        parse_mode='HTML',
        language=db_user.language,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_info, F.data == 'menu_support')
