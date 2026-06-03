import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import text

from app.database.database import AsyncSessionLocal
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts


logger = logging.getLogger(__name__)

_PERMANENT_DELIVERY_PHRASES = (
    "chat not found",
    "user is deactivated",
    "bot can't initiate conversation",
    "peer_id_invalid",
)

CHECK_INTERVAL_SECONDS = 60
WINDOW = timedelta(minutes=2)

SubscriptionType = Literal["trial", "paid"]
NotificationPhase = Literal["expiring_24h", "expiring_1h", "expired", "expired_24h"]
StatusScope = Literal["before_expiry", "after_expiry"]

NOTIFICATION_RULES = [
    {
        "subscription_type": "trial",
        "phase": "expiring_24h",
        "notification_type": "trial_expiring_24h",
        "minutes_before": 1440,
        "days_before": 1,
        "offset": timedelta(hours=24),
        "status_scope": "before_expiry",
    },
    {
        "subscription_type": "trial",
        "phase": "expiring_1h",
        "notification_type": "trial_expiring_1h",
        "minutes_before": 60,
        "days_before": 0,
        "offset": timedelta(hours=1),
        "status_scope": "before_expiry",
    },
    {
        "subscription_type": "trial",
        "phase": "expired",
        "notification_type": "trial_expired",
        "minutes_before": 0,
        "days_before": 0,
        "offset": timedelta(0),
        "status_scope": "after_expiry",
    },
    {
        "subscription_type": "trial",
        "phase": "expired_24h",
        "notification_type": "trial_expired_24h",
        "minutes_before": -1440,
        "days_before": -1,
        "offset": -timedelta(hours=24),
        "status_scope": "after_expiry",
    },
    {
        "subscription_type": "paid",
        "phase": "expiring_24h",
        "notification_type": "paid_expiring_24h",
        "minutes_before": 1440,
        "days_before": 1,
        "offset": timedelta(hours=24),
        "status_scope": "before_expiry",
    },
    {
        "subscription_type": "paid",
        "phase": "expiring_1h",
        "notification_type": "paid_expiring_1h",
        "minutes_before": 60,
        "days_before": 0,
        "offset": timedelta(hours=1),
        "status_scope": "before_expiry",
    },
    {
        "subscription_type": "paid",
        "phase": "expired",
        "notification_type": "paid_expired",
        "minutes_before": 0,
        "days_before": 0,
        "offset": timedelta(0),
        "status_scope": "after_expiry",
    },
    {
        "subscription_type": "paid",
        "phase": "expired_24h",
        "notification_type": "paid_expired_24h",
        "minutes_before": -1440,
        "days_before": -1,
        "offset": -timedelta(hours=24),
        "status_scope": "after_expiry",
    },
]

_TEXT_DEFAULTS: dict[str, dict[str, str]] = {
    "ru": {
        "EXPIRY_NOTIFY_TRIAL_24H": (
            "⏳ <b>Пробная подписка закончится через сутки</b>\n\n"
            "VPN пока работает. Чтобы доступ не прервался, можно заранее купить полную подписку."
        ),
        "EXPIRY_NOTIFY_TRIAL_1H": (
            "⏰ <b>Пробная подписка закончится через час</b>\n\n"
            "Если VPN нужен дальше, самое время перейти на платную подписку."
        ),
        "EXPIRY_NOTIFY_TRIAL_EXPIRED": (
            "❌ <b>Пробная подписка закончилась</b>\n\n"
            "Чтобы снова подключиться к VPN, купите подписку в боте."
        ),
        "EXPIRY_NOTIFY_TRIAL_EXPIRED_24H": (
            "💬 <b>Пробная подписка закончилась сутки назад</b>\n\n"
            "Доступ можно вернуть в пару шагов: выберите подписку и оплатите её в боте."
        ),
        "EXPIRY_NOTIFY_PAID_24H": (
            "⏳ <b>Подписка закончится через сутки</b>\n\n"
            "Продлите её заранее, чтобы VPN продолжил работать без перерыва."
        ),
        "EXPIRY_NOTIFY_PAID_1H": (
            "⏰ <b>Подписка закончится через час</b>\n\n"
            "Можно продлить её сейчас, чтобы не потерять доступ к VPN."
        ),
        "EXPIRY_NOTIFY_PAID_EXPIRED": (
            "❌ <b>Подписка закончилась</b>\n\n"
            "Возобновите подписку, чтобы снова пользоваться VPN."
        ),
        "EXPIRY_NOTIFY_PAID_EXPIRED_24H": (
            "💬 <b>Подписка закончилась сутки назад</b>\n\n"
            "Купите подписку заново, и доступ к VPN вернётся после оплаты."
        ),
    },
    "en": {
        "EXPIRY_NOTIFY_TRIAL_24H": (
            "⏳ <b>Your trial ends in 24 hours</b>\n\n"
            "VPN access is still active. Buy a full subscription now to keep it running without a break."
        ),
        "EXPIRY_NOTIFY_TRIAL_1H": (
            "⏰ <b>Your trial ends in 1 hour</b>\n\n"
            "If you want to keep using the VPN, now is a good time to switch to a paid subscription."
        ),
        "EXPIRY_NOTIFY_TRIAL_EXPIRED": (
            "❌ <b>Your trial has ended</b>\n\n"
            "Buy a subscription in the bot to restore VPN access."
        ),
        "EXPIRY_NOTIFY_TRIAL_EXPIRED_24H": (
            "💬 <b>Your trial ended 24 hours ago</b>\n\n"
            "You can restore VPN access in a few taps: choose a subscription and complete the payment."
        ),
        "EXPIRY_NOTIFY_PAID_24H": (
            "⏳ <b>Your subscription ends in 24 hours</b>\n\n"
            "Renew it in advance to keep VPN access running without interruption."
        ),
        "EXPIRY_NOTIFY_PAID_1H": (
            "⏰ <b>Your subscription ends in 1 hour</b>\n\n"
            "Renew now to avoid losing VPN access."
        ),
        "EXPIRY_NOTIFY_PAID_EXPIRED": (
            "❌ <b>Your subscription has expired</b>\n\n"
            "Renew your subscription to restore VPN access."
        ),
        "EXPIRY_NOTIFY_PAID_EXPIRED_24H": (
            "💬 <b>Your subscription expired 24 hours ago</b>\n\n"
            "Buy a new subscription, and VPN access will return after payment."
        ),
    },
}


def _normalize_language(language: str | None) -> str:
    language_code = (language or DEFAULT_LANGUAGE).split("-")[0].lower()
    return "en" if language_code == "en" else "ru"


def _notification_key(subscription_type: SubscriptionType, phase: NotificationPhase) -> str:
    suffix_by_phase = {
        "expiring_24h": "24H",
        "expiring_1h": "1H",
        "expired": "EXPIRED",
        "expired_24h": "EXPIRED_24H",
    }
    type_part = "TRIAL" if subscription_type == "trial" else "PAID"
    return f"EXPIRY_NOTIFY_{type_part}_{suffix_by_phase[phase]}"


def build_notification_text(
    subscription_type: SubscriptionType,
    phase: NotificationPhase,
    language: str | None = DEFAULT_LANGUAGE,
) -> str:
    normalized_language = _normalize_language(language)
    texts = get_texts(normalized_language)
    key = _notification_key(subscription_type, phase)
    return texts.t(key, _TEXT_DEFAULTS[normalized_language][key])


def build_notification_keyboard(
    subscription_type: SubscriptionType,
    phase: NotificationPhase,
    language: str | None = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    texts = get_texts(_normalize_language(language))

    if subscription_type == "trial" or phase == "expired_24h":
        action_text = texts.t("MENU_BUY_SUBSCRIPTION", "💳 Купить подписку")
    else:
        action_text = texts.t("MENU_RENEW_SUBSCRIPTION", "🔄 Продлить подписку")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=action_text, callback_data="subscription_upgrade")],
            [
                InlineKeyboardButton(
                    text=texts.t("BACK_TO_MAIN_MENU_BUTTON", "⬅️ В главное меню"),
                    callback_data="back_to_menu",
                )
            ],
        ]
    )


def _target_window(
    now: datetime,
    offset: timedelta,
    status_scope: StatusScope,
) -> tuple[datetime, datetime]:
    target_time = now + offset
    target_from = target_time - WINDOW
    target_to = target_time + WINDOW

    if status_scope == "after_expiry":
        target_to = min(target_to, now)

    return target_from, target_to


async def subscription_expiry_notifications_worker(bot):
    logger.info("Subscription expiry notifications worker started")

    while True:
        try:
            await check_subscription_expiry_notifications(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Subscription expiry notifications worker error")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def check_subscription_expiry_notifications(bot):
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        for rule in NOTIFICATION_RULES:
            subscription_type = rule["subscription_type"]
            phase = rule["phase"]
            notification_type = rule["notification_type"]
            minutes_before = rule["minutes_before"]
            days_before = rule["days_before"]
            offset = rule["offset"]
            status_scope = rule["status_scope"]

            target_from, target_to = _target_window(now, offset, status_scope)

            if subscription_type == "trial":
                trial_condition = "s.is_trial IS TRUE"
            else:
                trial_condition = "COALESCE(s.is_trial, FALSE) IS FALSE"

            if status_scope == "before_expiry":
                status_condition = "s.status IN ('active', 'trial', 'limited')"
                active_access_guard = ""
            else:
                status_condition = "s.status IN ('active', 'trial', 'limited', 'expired', 'disabled')"
                active_access_guard = """
                      AND NOT EXISTS (
                          SELECT 1
                          FROM subscriptions active_s
                          WHERE active_s.user_id = s.user_id
                            AND active_s.id <> s.id
                            AND active_s.end_date IS NOT NULL
                            AND active_s.end_date > :now
                            AND active_s.status IN ('active', 'trial', 'limited')
                      )
                """

            subscriptions_result = await db.execute(
                text(f"""
                    SELECT
                        s.id AS subscription_id,
                        s.user_id AS user_id,
                        s.end_date AS expires_at,
                        u.telegram_id AS telegram_id,
                        u.language AS language
                    FROM subscriptions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.end_date IS NOT NULL
                      AND s.end_date >= :target_from
                      AND s.end_date <= :target_to
                      AND {status_condition}
                      AND u.telegram_id IS NOT NULL
                      AND COALESCE(u.status, 'active') = 'active'
                      AND {trial_condition}
                      {active_access_guard}
                    ORDER BY s.end_date ASC
                    LIMIT 1000
                """),
                {
                    "target_from": target_from,
                    "target_to": target_to,
                    "now": now,
                },
            )

            subscriptions = subscriptions_result.mappings().all()

            for sub in subscriptions:
                subscription_id = sub["subscription_id"]
                user_id = sub["user_id"]
                telegram_id = sub["telegram_id"]
                expires_at = sub["expires_at"]
                language = sub["language"] or DEFAULT_LANGUAGE

                already_sent_result = await db.execute(
                    text("""
                        SELECT 1
                        FROM sent_notifications
                        WHERE user_id = :user_id
                          AND subscription_id = :subscription_id
                          AND notification_type = :notification_type
                          AND minutes_before = :minutes_before
                          AND expires_at = :expires_at
                        LIMIT 1
                    """),
                    {
                        "user_id": user_id,
                        "subscription_id": subscription_id,
                        "notification_type": notification_type,
                        "minutes_before": minutes_before,
                        "expires_at": expires_at,
                    },
                )

                if already_sent_result.scalar_one_or_none():
                    continue

                message_text = build_notification_text(
                    subscription_type=subscription_type,
                    phase=phase,
                    language=language,
                )
                keyboard = build_notification_keyboard(
                    subscription_type=subscription_type,
                    phase=phase,
                    language=language,
                )

                try:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=message_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    delivery_status = "sent"
                except TelegramForbiddenError:
                    delivery_status = "blocked"
                    logger.info(
                        "Skipping expiry notification: bot blocked by user — user_id=%s subscription_id=%s telegram_id=%s type=%s",
                        user_id,
                        subscription_id,
                        telegram_id,
                        notification_type,
                    )
                except TelegramBadRequest as exc:
                    msg = str(exc).lower()
                    if any(phrase in msg for phrase in _PERMANENT_DELIVERY_PHRASES):
                        delivery_status = "permanent_failure"
                        logger.info(
                            "Skipping expiry notification: permanent delivery failure '%s' — user_id=%s subscription_id=%s telegram_id=%s",
                            exc,
                            user_id,
                            subscription_id,
                            telegram_id,
                        )
                    else:
                        await db.rollback()
                        logger.warning(
                            "Transient TelegramBadRequest while sending expiry notification: %s — user_id=%s subscription_id=%s telegram_id=%s",
                            exc,
                            user_id,
                            subscription_id,
                            telegram_id,
                        )
                        continue
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "Failed to send subscription expiry notification: user_id=%s subscription_id=%s telegram_id=%s",
                        user_id,
                        subscription_id,
                        telegram_id,
                    )
                    continue

                try:
                    await db.execute(
                        text("""
                            INSERT INTO sent_notifications (
                                user_id,
                                subscription_id,
                                notification_type,
                                days_before,
                                minutes_before,
                                expires_at,
                                created_at
                            )
                            VALUES (
                                :user_id,
                                :subscription_id,
                                :notification_type,
                                :days_before,
                                :minutes_before,
                                :expires_at,
                                NOW()
                            )
                            ON CONFLICT (subscription_id, notification_type, minutes_before, expires_at)
                            DO NOTHING
                        """),
                        {
                            "user_id": user_id,
                            "subscription_id": subscription_id,
                            "notification_type": notification_type,
                            "days_before": days_before,
                            "minutes_before": minutes_before,
                            "expires_at": expires_at,
                        },
                    )

                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "Failed to record sent notification: user_id=%s subscription_id=%s type=%s status=%s",
                        user_id,
                        subscription_id,
                        notification_type,
                        delivery_status,
                    )
                    continue

                if delivery_status == "sent":
                    logger.info(
                        "Sent subscription expiry notification: user_id=%s subscription_id=%s type=%s minutes_before=%s",
                        user_id,
                        subscription_id,
                        notification_type,
                        minutes_before,
                    )
