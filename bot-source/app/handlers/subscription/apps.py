"""
Templar VPN — модуль "Скачать приложение" (раздел 2.14 ТЗ).
Поддерживает несколько приложений (INCY, Happ) и несколько платформ для каждого.

Поток:
  menu_apps                  -> выбор приложения [INCY] [Happ]
  apps_select_<app>          -> выбор платформы [iOS] [Android] [Windows] [macOS] [Linux]
  apps_dl_<app>_<platform>   -> кнопки-ссылки (одна или несколько)
"""
from aiogram import types
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.utils.banners import show_with_banner
from app.localization.texts import get_texts


APPS_CONFIG = {
    'incy': {
        'name': '📱 INCY',
        'short_name': 'INCY',
        'platforms': {
            'ios': {
                'label': '🍎 iOS',
                'links': [
                    {'label': '🔗 Открыть в App Store',
                     'url': 'https://apps.apple.com/ru/app/incy/id6756943388'},
                ],
            },
            'android': {
                'label': '🤖 Android',
                'links': [
                    {'label': '🔗 Открыть в Google Play',
                     'url': 'https://play.google.com/store/apps/details?id=llc.itdev.incy&pli=1'},
                ],
            },
            'windows': {
                'label': '💻 Windows',
                'links': [
                    {'label': '⬇️ Скачать .exe',
                     'url': 'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-windows-setup.exe'},
                ],
            },
            'macos': {
                'label': '🖥️ macOS',
                'links': [
                    {'label': '🍎 Apple Silicon (M1/M2/M3+)',
                     'url': 'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-macos-arm64.dmg'},
                    {'label': '🍏 Intel',
                     'url': 'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-macos-intel.dmg'},
                ],
            },
            'linux': {
                'label': '🐧 Linux',
                'links': [
                    {'label': '🔗 Открыть GitHub',
                     'url': 'https://github.com/INCY-DEV/incy-platforms'},
                ],
            },
        },
    },
    'happ': {
        'name': '📱 Happ (рекомендуем)',
        'short_name': 'Happ',
        'platforms': {
            'ios': {
                'label': '🍎 iOS',
                'links': [
                    {'label': '🇷🇺 App Store Россия (Plus)',
                     'url': 'https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'},
                    {'label': '🌐 App Store Global',
                     'url': 'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215'},
                ],
            },
            'android': {
                'label': '🤖 Android',
                'links': [
                    {'label': '🔗 Открыть в Google Play',
                     'url': 'https://play.google.com/store/apps/details?id=com.happproxy'},
                ],
            },
            'windows': {
                'label': '💻 Windows',
                'links': [
                    {'label': '⬇️ Скачать .exe',
                     'url': 'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe'},
                ],
            },
            'macos': {
                'label': '🖥️ macOS',
                'links': [
                    {'label': '⬇️ Скачать .dmg',
                     'url': 'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg'},
                ],
            },
            'linux': {
                'label': '🐧 Linux',
                'links': [
                    {'label': '🔗 Открыть happ.su',
                     'url': 'https://www.happ.su/main'},
                ],
            },
        },
    },
}


APPS_LINK_LABEL_KEYS = {
    '🔗 Открыть в App Store': 'APPS_LINK_APP_STORE',
    '🇷🇺 App Store Россия (Plus)': 'APPS_LINK_APP_STORE_RU_PLUS',
    '🌐 App Store Global': 'APPS_LINK_APP_STORE_GLOBAL',
    '🔗 Открыть в Google Play': 'APPS_LINK_GOOGLE_PLAY',
    '⬇️ Скачать .exe': 'APPS_LINK_DOWNLOAD_EXE',
    '⬇️ Скачать .dmg': 'APPS_LINK_DOWNLOAD_DMG',
    '🍎 Apple Silicon (M1/M2/M3+)': 'APPS_LINK_DMG_APPLE_SILICON',
    '🍏 Intel': 'APPS_LINK_DMG_INTEL',
    '🔗 Открыть GitHub': 'APPS_LINK_OPEN_GITHUB',
    '🔗 Открыть happ.su': 'APPS_LINK_OPEN_HAPP_SU',
}


async def handle_menu_apps(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    callback_auto_answered: bool = False,
):
    """Главный экран — выбор приложения."""
    if isinstance(callback.message, InaccessibleMessage):
        if not callback_auto_answered:
            await callback.answer()
        return

    texts = get_texts(db_user.language)
    text = texts.t(
        'APPS_SELECT_PROMPT',
        '📲 <b>Скачать приложение</b>\n\nВыберите приложение для подключения VPN:',
    )

    buttons = []
    # Порядок отображения: сначала рекомендованное приложение
    apps_order = ['happ', 'incy']
    for app_id in apps_order:
        app = APPS_CONFIG.get(app_id)
        if not app:
            continue
        buttons.append([InlineKeyboardButton(text=texts.t(f'APP_NAME_{app_id.upper()}', app['name']), callback_data=f'apps_select_{app_id}')])
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await show_with_banner(callback, 'download', text, keyboard, 'HTML', language=db_user.language)
    if not callback_auto_answered:
        await callback.answer()


async def handle_apps_select(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    callback_auto_answered: bool = False,
):
    """Выбор платформы для приложения."""
    if isinstance(callback.message, InaccessibleMessage):
        if not callback_auto_answered:
            await callback.answer()
        return

    texts = get_texts(db_user.language)
    app_id = callback.data.replace('apps_select_', '', 1)
    app = APPS_CONFIG.get(app_id)
    if not app:
        await callback.answer(texts.t('APP_NOT_FOUND', 'Приложение не найдено'), show_alert=True)
        return

    text = texts.t(
        'APPS_PLATFORM_PROMPT',
        texts.t('APP_SELECT_PLATFORM_PROMPT', '📲 <b>{app}</b>\n\nВыберите вашу платформу:'),
    ).format(app=app['short_name'])

    buttons = []
    for platform_id, platform in app['platforms'].items():
        buttons.append([
            InlineKeyboardButton(
                text=texts.t(f'APPS_PLATFORM_{platform_id.upper()}', platform['label']),
                callback_data=f'apps_dl_{app_id}_{platform_id}',
            )
        ])
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_apps')])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await show_with_banner(callback, 'download', text, keyboard, 'HTML', language=db_user.language)
    if not callback_auto_answered:
        await callback.answer()


async def handle_apps_platform(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    callback_auto_answered: bool = False,
):
    """Показ кнопок-ссылок для пары приложение+платформа."""
    if isinstance(callback.message, InaccessibleMessage):
        if not callback_auto_answered:
            await callback.answer()
        return

    texts = get_texts(db_user.language)
    payload = callback.data.replace('apps_dl_', '', 1)
    parts = payload.split('_', 1)
    if len(parts) != 2:
        await callback.answer(texts.t('APP_INVALID_REQUEST', 'Некорректный запрос'), show_alert=True)
        return

    app_id, platform_id = parts
    app = APPS_CONFIG.get(app_id)
    if not app:
        await callback.answer(texts.t('APP_NOT_FOUND', 'Приложение не найдено'), show_alert=True)
        return
    platform = app['platforms'].get(platform_id)
    if not platform:
        await callback.answer(texts.t('APP_PLATFORM_NOT_FOUND', 'Платформа не найдена'), show_alert=True)
        return

    text = texts.t(
        'APPS_DOWNLOAD_PROMPT',
        texts.t('APP_DOWNLOAD_PROMPT', '⬇️ <b>{app} для {platform}</b>\n\nНажмите на кнопку ниже, чтобы открыть ссылку:'),
    ).format(app=app['short_name'], platform=texts.t(f'APPS_PLATFORM_{platform_id.upper()}', platform['label']))

    buttons = []
    for link in platform['links']:
        _link_key = APPS_LINK_LABEL_KEYS.get(link['label'])
        _link_text = texts.t(_link_key, link['label']) if _link_key else link['label']
        buttons.append([InlineKeyboardButton(text=_link_text, url=link['url'])])
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'apps_select_{app_id}')])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await show_with_banner(callback, 'download', text, keyboard, 'HTML', language=db_user.language)
    if not callback_auto_answered:
        await callback.answer()
