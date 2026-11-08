"""Фоновые напоминания .

Цикл каждые INTERVAL_SECONDS проверяет пользователей с reminders_enabled:
- ученикам — ежедневный опрос «Как ты сегодня?» в настроенное время,
  напоминания о домашке и дедлайнах за 3 дня, за неделю и за день до срока
  (каждый срок можно отключить в настройках: notify_3d/notify_7d/notify_1d);
- родителям — учебные события ребёнка по тем же срокам.

Доставка каждого напоминания отмечается в ReminderDelivery, чтобы
не отправлять его повторно.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.services import mvp

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 900
DEFAULT_SURVEY_HOUR = 18
SURVEY_HOUR_MIN = 8
SURVEY_HOUR_MAX = 22

# Сроки напоминаний о дедлайнах (в днях) и соответствующие флаги настроек.
NOTIFY_LEVELS: tuple[int, ...] = (7, 3, 1)
NOTIFY_FLAGS: dict[int, str] = {7: "notify_7d", 3: "notify_3d", 1: "notify_1d"}
NOTIFY_LABELS: dict[int, str] = {
    0: "сегодня",
    1: "завтра",
    3: "через 3 дня",
    7: "через неделю",
}

Sender = Callable[[int, str], Awaitable[None]]


def survey_hour_for(user: Any) -> int:
    hour = getattr(user, "mood_survey_hour", None)
    if hour is None:
        return DEFAULT_SURVEY_HOUR
    return max(SURVEY_HOUR_MIN, min(SURVEY_HOUR_MAX, int(hour)))


def enabled_notify_levels(user: Any) -> set[int]:
    """Сроки (в днях), за которые пользователю приходят напоминания."""
    return {
        days
        for days in NOTIFY_LEVELS
        if bool(getattr(user, NOTIFY_FLAGS[days], True))
    }


def _notify_label(days_left: int) -> str:
    return NOTIFY_LABELS.get(days_left, f"через {days_left} дн.")


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


async def build_student_reminders(user: Any, now: datetime) -> list[tuple[str, str]]:
    """Список (delivery_key, текст) напоминаний для ученика."""
    reminders: list[tuple[str, str]] = []
    if now.hour >= survey_hour_for(user) and not await mvp.mood_was_logged_today(user.id):
        reminders.append(
            (
                f"mood:{now:%Y-%m-%d}",
                "🙂 Как ты сегодня? Открой «🧘 Антистресс» и отметь настроение — "
                "это займёт 5 секунд.",
            )
        )
    levels = enabled_notify_levels(user)
    today = now.date()
    for item in await mvp.upcoming_deadlines(user):
        due = _naive(item.date)
        days_left = (due.date() - today).days
        if days_left not in levels and not (days_left == 0 and 1 in levels):
            continue
        reminders.append(
            (
                f"deadline:{item.id}:{days_left}d",
                f"⏰ Напоминание: {_notify_label(days_left)} «{item.title}» "
                f"({item.kind}) в {due:%H:%M} ({due:%d.%m.%Y}).",
            )
        )
    for item, done in await mvp.active_homework(user):
        if done:
            continue
        due = _naive(item.deadline)
        days_left = (due.date() - today).days
        if days_left not in levels and not (days_left == 0 and 1 in levels):
            continue
        reminders.append(
            (
                f"homework:{item.id}:{days_left}d",
                f"📝 Не забудь сдать {_notify_label(days_left)}: {item.subject} — "
                f"{item.description} (срок {due:%d.%m.%Y %H:%M}).",
            )
        )
    return reminders


async def build_parent_reminders(user: Any, now: datetime) -> list[tuple[str, str]]:
    """Список (delivery_key, текст) уведомлений о важных учебных событиях."""
    reminders: list[tuple[str, str]] = []
    levels = enabled_notify_levels(user)
    today = now.date()
    for item in await mvp.deadlines_for_parent(user):
        due = _naive(item.date)
        days_left = (due.date() - today).days
        if days_left not in levels and not (days_left == 0 and 1 in levels):
            continue
        reminders.append(
            (
                f"parent_deadline:{item.id}:{days_left}d",
                f"🔔 Учебное событие у ребёнка {_notify_label(days_left)}: "
                f"«{item.title}» ({item.kind}) — {due:%d.%m.%Y %H:%M}.",
            )
        )
    return reminders


async def run_once(sender: Sender, now: datetime | None = None) -> int:
    """Один проход планировщика. Возвращает число отправленных напоминаний."""
    now = now or datetime.now()
    users = await mvp.reminder_users()
    sent = 0
    for user in users:
        role = getattr(user.role, "value", str(user.role))
        if role == "student":
            reminders = await build_student_reminders(user, now)
        elif role == "parent":
            reminders = await build_parent_reminders(user, now)
        else:
            continue
        for delivery_key, text in reminders:
            if await mvp.reminder_already_delivered(user.id, delivery_key):
                continue
            try:
                target_user_id = int(user.max_user_id)
            except (ValueError, TypeError):
                logger.warning(
                    "Некорректный max_user_id у пользователя %s: %r",
                    user.id,
                    user.max_user_id,
                )
                continue
            try:
                await sender(target_user_id, text)
            except Exception:
                logger.exception(
                    "Не удалось отправить напоминание %s пользователю %s",
                    delivery_key,
                    user.id,
                )
                continue
            await mvp.record_reminder_delivery(user.id, delivery_key)
            sent += 1
    return sent


async def run_forever(sender: Sender, interval: int = INTERVAL_SECONDS) -> None:
    logger.info("Планировщик напоминаний запущен (интервал %s c).", interval)
    while True:
        try:
            await run_once(sender)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Цикл напоминаний завершился с ошибкой")
        await asyncio.sleep(interval)

