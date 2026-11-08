from __future__ import annotations

import asyncio
import logging
import random
import re
from contextvars import ContextVar
from datetime import datetime, time
from typing import Any

from maxapi import Bot, Dispatcher
from maxapi.enums.attachment import AttachmentType
from maxapi.types import Attachment, ButtonsPayload

from app import config
from app.db import crud
from app.db.database import close_database, create_db_and_tables
from app.db.models import RequestStatus, User, UserRole
from app.keyboards.inline import (
    grade_keyboard,
    keyboard,
    menu_keyboard,
    mini_app_keyboard,
    registration_roles_keyboard,
    staff_roles_keyboard,
    with_menu_row,
)
from app.mini_web.auth import make_token
from app.services import ai_psychologist, mvp, scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = config.BOT_TOKEN
bot = Bot(token=TOKEN or "missing-token")
dp = Dispatcher()
_states: dict[str, dict[str, Any]] = {}
# Роль пользователя в текущем обработчике события: позволяет _send автоматически
# добавлять кнопку «⬅️ В меню» на каждый экран кабинета (без тупиков).
_role_ctx: ContextVar[str | None] = ContextVar("hq_role_ctx", default=None)
# Настоящий MAX ID автора события: нужен, чтобы dev-команды работали даже тогда,
# когда разработчик «вошёл как» тестовый аккаунт.
_real_id_ctx: ContextVar[str | None] = ContextVar("hq_real_id_ctx", default=None)
# Dev-режим «войти как»: настоящий MAX ID → MAX ID тестового аккаунта.
# Один MAX ID = одна роль, поэтому проверить путь «ученик пишет психологу»
# с одного аккаунта иначе невозможно.
_impersonation: dict[str, str] = {}


def _effective_max_id(max_id: str) -> str:
    """Чей аккаунт играем: свой или тестовый (доступно только команде)."""
    if _is_dev(max_id) and max_id in _impersonation:
        return _impersonation[max_id]
    return max_id


def _is_impersonating() -> bool:
    """Играем ли сейчас тестовый аккаунт (нужно для кнопки выхода в меню)."""
    real = _real_id_ctx.get()
    return bool(real) and real in _impersonation

ROLE_LABELS = {
    "student": "ученика",
    "teacher": "учителя",
    "parent": "родителя",
    "psychologist": "психолога",
}

# Скрытая панель команды разработчиков: смена любой роли в любой момент.
# Обычные пользователи не имеют к ней доступа и не видят никаких следов.
DEV_ROLE_TITLES = {
    "student": "🎓 Ученик",
    "teacher": "👩‍🏫 Учитель",
    "parent": "👨‍👩‍👧 Родитель",
    "psychologist": "🧠 Психолог",
}


def _is_dev(max_id: Any) -> bool:
    """Входит ли пользователь в список команды разработчиков (config.DEV_MAX_IDS)."""
    try:
        return int(max_id) in config.DEV_MAX_IDS
    except (TypeError, ValueError):
        return False


def _missing_full_name(user: User) -> bool:
    return not user.full_name.strip() or user.full_name.strip() == "Пользователь"


def _max_user_id(event: Any) -> str | None:
    users = (
        getattr(getattr(event, "callback", None), "user", None),
        getattr(event, "from_user", None),
        getattr(getattr(event, "message", None), "sender", None),
    )
    for user in users:
        user_id = getattr(user, "user_id", None)
        if user_id is not None:
            return str(user_id)
    return None


def _chat_id(event: Any) -> int | None:
    chat = getattr(event, "chat", None)
    chat_id = getattr(chat, "chat_id", None)
    if chat_id is not None:
        return chat_id
    recipient = getattr(getattr(event, "message", None), "recipient", None)
    return getattr(recipient, "chat_id", None)


async def _send(
    chat_id: int | None,
    text: str,
    buttons: Any | None = None,
    user_id: int | None = None,
    auto_menu: bool = True,
) -> None:
    role = _role_ctx.get()
    if auto_menu and role is not None and user_id is None:
        # Кнопка выхода в меню должна быть на каждом экране кабинета.
        buttons = with_menu_row(buttons, role)
    await bot.send_message(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        attachments=[
            Attachment(type=AttachmentType.INLINE_KEYBOARD, payload=buttons)
        ]
        if buttons is not None
        else None,
    )


async def _typing(chat_id: int | None) -> None:
    """Показать индикатор «печатает…», пока бот готовит ответ."""
    if chat_id is None:
        return
    try:
        await bot.send_action(chat_id=chat_id)
    except Exception:  # noqa: BLE001 - индикатор не должен ломать ответ
        logger.debug("Не удалось показать индикатор набора", exc_info=True)


async def _set_state(user: User, step: str | None, **data: Any) -> None:
    key = str(user.max_user_id)
    if step is None:
        _states.pop(key, None)
    else:
        _states[key] = {"step": step, **data}


def _get_state(max_id: str) -> dict[str, Any] | None:
    return _states.get(str(max_id))


def _role_value(role: UserRole | str) -> str:
    return role.value if isinstance(role, UserRole) else str(role)


def _mini_app_url(user: User) -> str:
    """Персональная ссылка на мини веб-приложение (подписанный токен доступа)."""
    base = config.MINI_APP_URL.rstrip("/")
    return f"{base}/mini?token={make_token(user.max_user_id)}"


_bot_username: str | None = None


async def _resolve_bot_username() -> str | None:
    """Username для нативной кнопки open_app.

    Нативная кнопка имеет смысл только если мини-приложение зарегистрировано на
    dev.max.ru, поэтому username берём исключительно из MINI_APP_BOT_NAME:
    автоопределение через GET /me давало username бота, приложение к которому
    не привязано — MAX не мог его открыть. Без настройки шлём кнопку-ссылку.
    """
    global _bot_username
    configured = (config.MINI_APP_BOT_NAME or "").strip().lstrip("@")
    _bot_username = configured or None
    return _bot_username


def _mini_app_args(user: User) -> dict[str, Any]:
    """Параметры кнопки мини-приложения.

    Токен передаётся ТОЛЬКО в web_url: MAX ограничивает payload кнопки
    (~64 символа), а подписанный токен длиннее — сервер отвечал
    «400 proto.payload: Field 'payload' does not match required pattern»,
    из-за чего главное меню вообще не отправлялось.
    """
    return {
        "web_url": _mini_app_url(user),
        "app_name": _bot_username,
        "payload": None,
    }


async def _menu(chat_id: int | None, user: User, max_id: int | None) -> None:
    role = _role_value(user.role)
    # Первая кнопка меню открывает мини-приложение прямо в MAX.
    buttons = menu_keyboard(role, **_mini_app_args(user))
    if _is_impersonating():
        # Разработчик играет тестовый аккаунт — выход должен быть на виду.
        rows = [[keyboard((("🎭 Выйти из тестового аккаунта", "dev:impersonate_exit"),)).buttons[0][0]]]
        rows += [list(row) for row in (buttons.buttons or [])]
        buttons = ButtonsPayload(buttons=rows)
    await _send(
        chat_id,
        f"Кабинет {ROLE_LABELS.get(role, 'пользователя')}. Выберите действие:",
        buttons,
        auto_menu=False,
    )


def _restart_keyboard(user: User | None) -> Any:
    """Кнопки выхода из тупика: в свой кабинет или к выбору роли.

    Пользователь не должен оставаться без кнопок и писать /start вручную.
    """
    role = _role_value(user.role) if user is not None else "unselected"
    if role in ROLE_LABELS:
        return keyboard((("🏠 Главное меню", f"menu:{role}"),))
    return keyboard((("↩️ Начать заново", "onboard:restart"),))


async def _ensure_approved(user: User | None) -> User | None:
    """Аккаунт активен сразу: подчищаем статусы, доставшиеся от старой версии.

    Администратора в продукте нет, поэтому «ожидающих» и «отклонённых» быть не
    должно. Если строку в БД обновить не удалось (или её нет), активируем хотя
    бы текущий объект — пользователь не должен упираться в отказ.
    """
    if user is None or user.status == RequestStatus.APPROVED:
        return user
    try:
        fresh = await mvp.approve_user(str(user.max_user_id))
    except Exception:  # noqa: BLE001 - доступ важнее записи в БД
        logger.exception("Could not activate account %s", user.max_user_id)
        fresh = None
    if fresh is None:
        user.status = RequestStatus.APPROVED
        return user
    return fresh




async def _send_link_request(link_id: int, parent: User, student: User) -> None:
    buttons = keyboard(
        (
            ("✅ Подтвердить", f"link_approve:{link_id}"),
            ("❌ Отклонить", f"link_reject:{link_id}"),
        ),
        (("⬅️ В меню", f"menu:{_role_value(student.role)}"),),
    )
    await _send(
        None,
        f"🔔 Пользователь {parent.name} хочет привязать ваш аккаунт как родитель. "
        "Вы подтверждаете?",
        buttons,
        user_id=int(student.max_user_id),
    )


async def _handle_input_step(
    chat_id: int | None,
    max_id: int,
    user: User,
    step: str,
    text: str,
    state: dict[str, Any],
) -> bool:
    value = text.strip()
    if not value:
        await _send(chat_id, "Пожалуйста, отправьте непустое значение.")
        return True

    if value.lower() in {"отмена", "cancel", "/cancel"}:
        await _set_state(user, None)
        await _send(
            chat_id,
            "Отменено. Напиши /start, чтобы открыть главное меню или начать заново.",
        )
        return True

    if step in {"sch_day", "sch_time", "sch_subject", "sch_room"} and (
        user.role != UserRole.TEACHER
    ):
        await _set_state(user, None)
        await _send(
            chat_id,
            "Расписание класса добавляет только учитель. Ученики могут смотреть "
            "его в разделе «📚 Расписание».",
            menu_keyboard(_role_value(user.role)),
        )
        return True

    if step in {
        "hw_subject",
        "hw_description",
        "hw_deadline",
        "hw_points",
        "dl_title",
        "dl_date",
    } and (user.role != UserRole.TEACHER):
        await _set_state(user, None)
        await _send(
            chat_id,
            "Домашку и дедлайны задаёт учитель по своему предмету. Ученики "
            "видят их в разделах «📝 Домашка» и «⏰ Дедлайны» и получают "
            "напоминания за неделю, за 3 дня и за день до срока.",
            menu_keyboard(_role_value(user.role)),
        )
        return True

    if step == "register_name":
        if len(value) > 255:
            await _send(chat_id, "Имя слишком длинное. Введите ФИО (до 255 символов).")
            return True
        await crud.save_unselected_user(str(max_id), value)
        await _set_state(user, None)
        await _send(
            chat_id,
            f"Отлично, {value}! Теперь выбери, кто ты в школе \ud83d\udc47\n"
            "(после выбора роль изменить не получится)",
            registration_roles_keyboard(),
        )
        return True

    if step == "onboarding_staff_name":
        if len(value) > 255:
            await _send(chat_id, "ФИО слишком длинное. Введите ФИО (до 255 символов).")
            return True
        await mvp.update_profile(str(max_id), full_name=value)
        await _set_state(
            user,
            "onboarding_psychologist_school"
            if user.role == UserRole.PSYCHOLOGIST
            else "onboarding_school",
        )
        await _send(chat_id, "В какой школе работаешь?")
        return True

    if step == "onboarding_classes":
        raw_parts = [part.strip() for part in value.split(",") if part.strip()]
        parts = []
        for part in raw_parts:
            if getattr(user, "grade", None) and not any(ch.isdigit() for ch in part):
                parts.append(f"{user.grade}{part.upper()}")
            else:
                parts.append(part)
        classes = ",".join(parts)
        if len(classes) > 255:
            await _send(chat_id, "Слишком длинное значение. Укажи класс, например 9А.")
            return True
        await mvp.update_profile(str(max_id), classes=classes, class_name=classes)
        await _set_state(user, None)
        await _menu(chat_id, user, max_id)
        return True

    if step == "onboarding_school":
        if len(value) > 255:
            await _send(chat_id, "Название школы слишком длинное.")
            return True
        await mvp.update_profile(str(max_id), school=value)
        user.school_name = value
        await _set_state(user, None)
        await _send(
            chat_id,
            "Школа сохранена. Добро пожаловать в личный кабинет сотрудника!",
        )
        await _menu(chat_id, user, max_id)
        return True

    if step == "onboarding_psychologist_school":
        if len(value) > 255:
            await _send(chat_id, "Название школы слишком длинное.")
            return True
        await mvp.update_profile(str(max_id), school=value, status="approved")
        await _set_state(user, None)
        user = await mvp.user_by_max_id(str(max_id)) or user
        await _send(
            chat_id,
            "Школа сохранена. Добро пожаловать в личный кабинет психолога!",
        )
        await _menu(chat_id, user, max_id)
        return True

    if step == "onboarding_parent_child":
        candidates = await crud.student_candidates(value)
        if not candidates:
            await _send(
                chat_id,
                "Ученик не найден. Проверьте ФИО или MAX ID из профиля ученика и попробуйте еще раз.",
            )
            return True
        if len(candidates) > 1:
            options = tuple(
                (
                    f"{item.name} — {item.class_name or 'класс не указан'}, "
                    f"MAX ID {item.max_user_id}",
                    f"parent:select_child:{item.id}",
                )
                for item in candidates
            )
            await _send(
                chat_id,
                "Найдено несколько учеников. Выберите нужного:",
                keyboard(*((option,) for option in options)),
            )
            return True
        await _request_link(chat_id, user, candidates[0])
        await _set_state(user, None)
        return True

    if step == "teacher_class_name":
        if len(value) > 64:
            await _send(chat_id, "Название класса должно быть не длиннее 64 символов.")
            return True
        await _set_state(user, "teacher_class_login", class_name=value)
        await _send(chat_id, "Придумайте логин для входа в класс.")
        return True

    if step == "teacher_class_login":
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", value):
            await _send(
                chat_id,
                "Логин должен содержать 3–64 латинских буквы, цифры, дефис или подчеркивание.",
            )
            return True
        await _set_state(
            user,
            "teacher_class_password",
            class_name=state["class_name"],
            login=value.lower(),
        )
        await _send(chat_id, "Задайте пароль для входа (не менее 8 символов).")
        return True

    if step == "teacher_class_password":
        if len(value) < 8 or len(value.encode("utf-8")) > 256:
            await _send(chat_id, "Пароль должен содержать от 8 до 256 байт.")
            return True
        try:
            school_class = await crud.create_school_class(
                user.id,
                state["class_name"],
                state["login"],
                value,
            )
        except (ValueError, PermissionError, RuntimeError) as error:
            await _set_state(user, None)
            await _send(chat_id, str(error))
            return True
        await _set_state(user, None)
        await _send(
            chat_id,
            f"Класс {school_class.name} создан.\n"
            f"Логин: {school_class.login}\n"
            f"Пароль: {value}\n"
            "Сохраните его и передайте ученикам.",
        )
        return True

    if step == "student_join_login":
        if len(value) > 64:
            await _send(chat_id, "Логин не найден. Проверьте его и попробуйте снова.")
            return True
        await _set_state(user, "student_join_password", login=value)
        await _send(chat_id, "Введите пароль класса.")
        return True

    if step == "student_join_password":
        try:
            message = await crud.join_school_class(
                user.id,
                state["login"],
                value,
            )
        except ValueError:
            await _send(
                chat_id,
                "Пароль не подошёл \ud83d\ude15 Проверь ввод и попробуй ещё раз. "
                "Если хочешь выйти, напиши /start.",
            )
            return True
        except PermissionError as error:
            await _set_state(user, None)
            await _send(chat_id, str(error))
            return True
        await _set_state(user, None)
        await _send(chat_id, message)
        return True

    if step == "hw_subject":
        if len(value) > 128:
            await _send(chat_id, "Название предмета должно быть не длиннее 128 символов.")
            return True
        await _set_state(
            user, "hw_description", class_name=state.get("class_name"), subject=value
        )
        await _send(chat_id, "Введите задание. Например: «§12, задачи 5-10».")
        return True

    if step == "hw_description":
        await _set_state(
            user,
            "hw_deadline",
            class_name=state.get("class_name"),
            subject=state["subject"],
            description=value,
        )
        await _send(
            chat_id,
            "Когда сдать? Формат: ДД.ММ.ГГГГ [ЧЧ:ММ], например 25.10.2026 23:59.",
        )
        return True

    if step == "hw_deadline":
        try:
            due = _parse_datetime(value)
        except ValueError as error:
            await _send(chat_id, str(error))
            return True
        await _set_state(
            user,
            "hw_points",
            class_name=state.get("class_name"),
            subject=state["subject"],
            description=state["description"],
            due=due,
        )
        await _send(
            chat_id,
            "Сколько баллов получит ученик за сдачу этой домашки? "
            "Выбери кнопкой или введи целое число от 0 до 100.",
            keyboard(
                (
                    ("5", "teacher:hw_points:5"),
                    ("10", "teacher:hw_points:10"),
                    ("15", "teacher:hw_points:15"),
                ),
                (
                    ("20", "teacher:hw_points:20"),
                    ("30", "teacher:hw_points:30"),
                    ("50", "teacher:hw_points:50"),
                ),
            ),
        )
        return True

    if step == "hw_points":
        try:
            points = int(value)
        except ValueError:
            points = -1
        if points not in range(0, 101):
            await _send(
                chat_id,
                "Введи целое число баллов от 0 до 100, например 15.",
                keyboard(
                    (
                        ("5", "teacher:hw_points:5"),
                        ("10", "teacher:hw_points:10"),
                        ("15", "teacher:hw_points:15"),
                    ),
                    (
                        ("20", "teacher:hw_points:20"),
                        ("30", "teacher:hw_points:30"),
                        ("50", "teacher:hw_points:50"),
                    ),
                ),
            )
            return True
        await _create_homework(chat_id, user, state, points)
        return True

    if step in {"psy_school", "stress_school"}:
        school = value[:255]
        await mvp.update_profile(str(user.max_user_id), school=school)
        user.school_name = school
        action = state.get("stress_format") or "chat"
        await _set_state(user, None)
        await _start_stress_signal(chat_id, user, action)
        return True

    if step == "set_school":
        school = value[:255]
        await mvp.update_profile(str(user.max_user_id), school=school)
        user.school_name = school
        await _set_state(user, None)
        role = _role_value(user.role)
        rows: list[tuple[tuple[str, str], ...]] = []
        if role == "student":
            # Школа нужна, чтобы найти психолога — сразу даём продолжить.
            rows.append((("💬 Поговорить с психологом", "student:talk_psychologist"),))
        if role in SETTINGS_PAYLOAD_BY_ROLE:
            rows.append((("⚙️ Настройки", SETTINGS_PAYLOAD_BY_ROLE[role]),))
        rows.append((("⬅️ В меню", f"menu:{role}"),))
        await _send(chat_id, f"🏫 Школа сохранена: {school}", keyboard(*rows) if rows else None)
        return True

    if step == "set_class":
        class_name = value[:64]
        await mvp.update_profile(str(user.max_user_id), class_name=class_name)
        user.class_name = class_name
        await _set_state(user, None)
        role = _role_value(user.role)
        rows: list[tuple[tuple[str, str], ...]] = []
        if role == "student":
            rows.append((("🏫 Войти в класс", "student:join_class"),))
        if role in SETTINGS_PAYLOAD_BY_ROLE:
            rows.append((("⚙️ Настройки", SETTINGS_PAYLOAD_BY_ROLE[role]),))
        rows.append((("⬅️ В меню", f"menu:{role}"),))
        await _send(chat_id, f"🎓 Класс сохранён: {class_name}", keyboard(*rows) if rows else None)
        return True

    if step == "sch_day":
        try:
            day = _day_number(value)
        except ValueError as error:
            await _send(chat_id, str(error))
            return True
        await _set_state(user, "sch_time", class_name=state.get("class_name"), day=day)
        await _send(
            chat_id,
            f"{_weekday_label(day)}. Введите время урока в формате ЧЧ:ММ, например 08:30.",
        )
        return True

    if step == "sch_time":
        try:
            lesson_time = _parse_lesson_time(value)
        except ValueError as error:
            await _send(chat_id, str(error))
            return True
        await _set_state(
            user,
            "sch_subject",
            class_name=state.get("class_name"),
            day=state["day"],
            lesson_time=lesson_time,
        )
        await _send(chat_id, "Введите предмет, например «Алгебра».")
        return True

    if step == "sch_subject":
        if len(value) > 128:
            await _send(chat_id, "Название предмета должно быть не длиннее 128 символов.")
            return True
        await _set_state(
            user,
            "sch_room",
            class_name=state.get("class_name"),
            day=state["day"],
            lesson_time=state["lesson_time"],
            subject=value,
        )
        await _send(chat_id, "Введите кабинет или «-», если его нет.")
        return True

    if step == "sch_room":
        room = None if value in {"-", "—", "нет"} else value[:64]
        class_name = state.get("class_name")
        day = state["day"]
        lesson_time = state["lesson_time"]
        if not class_name:
            await _set_state(user, None)
            await _send(
                chat_id,
                "Класс не выбран. Начните заново: меню → 🗓 Расписание → класс.",
                menu_keyboard("teacher"),
            )
            return True
        await mvp.create_user_schedule_for_class(
            user, class_name, day, lesson_time, state["subject"], room
        )
        await _set_state(user, None)
        room_part = f", каб. {room}" if room else ""
        await _send(
            chat_id,
            f"✅ Урок добавлен в расписание класса {class_name}: "
            f"{_weekday_label(day)}, {lesson_time:%H:%M} — {state['subject']}{room_part}",
        )
        await _show_teacher_class_schedule(chat_id, user, class_name)
        return True

    if step == "survey_time":
        match = re.fullmatch(r"(\d{1,2})(?::00)?", value)
        hour = int(match.group(1)) if match else None
        if hour is None or not (
            scheduler.SURVEY_HOUR_MIN <= hour <= scheduler.SURVEY_HOUR_MAX
        ):
            await _send(
                chat_id,
                f"Введи час от {scheduler.SURVEY_HOUR_MIN} до "
                f"{scheduler.SURVEY_HOUR_MAX}, например 18.",
            )
            return True
        await _save_survey_hour(chat_id, str(max_id), user, hour)
        return True

    if step == "dl_title":
        if len(value) > 255:
            await _send(chat_id, "Название слишком длинное (до 255 символов).")
            return True
        await _set_state(user, "dl_date", class_name=state.get("class_name"), title=value)
        await _send(
            chat_id,
            "Введите дату и время: ДД.ММ.ГГГГ [ЧЧ:ММ], например 25.10.2026 10:00.",
        )
        return True

    if step == "dl_date":
        try:
            due = _parse_datetime(value)
        except ValueError as error:
            await _send(chat_id, str(error))
            return True
        prefix = "teacher"
        await _set_state(
            user,
            "dl_kind",
            class_name=state.get("class_name"),
            title=state["title"],
            due=due,
        )
        await _send(
            chat_id,
            "Выберите тип:",
            keyboard(
                (
                    ("Контрольная", f"{prefix}:dl_kind:контрольная"),
                    ("Самостоятельная", f"{prefix}:dl_kind:самостоятельная"),
                ),
                (("Проект", f"{prefix}:dl_kind:проект"), ("Зачёт", f"{prefix}:dl_kind:зачёт")),
            ),
        )
        return True

    if step == "announce_text":
        class_name = state["class_name"]
        await mvp.create_announcement(user.id, class_name, value)
        recipients = await mvp.announcement_recipients(user.id, class_name)
        delivered = 0
        for student in recipients:
            if str(student.max_user_id) == str(user.max_user_id):
                continue
            try:
                await _send(
                    None,
                    f"📢 Объявление класса {class_name}:\n{value}",
                    keyboard(
                        (("📢 Объявления класса", "student:announcements"),),
                        (("⬅️ В меню", "menu:student"),),
                    ),
                    user_id=int(student.max_user_id),
                )
                delivered += 1
            except Exception:  # noqa: BLE001 - сбой доставки не отменяет объявление
                logger.warning(
                    "Не удалось доставить объявление ученику %s", student.id
                )
        await _set_state(user, None)
        await _send(
            chat_id,
            f"📢 Объявление отправлено классу {class_name}: "
            f"доставлено учеников: {delivered}.",
            menu_keyboard("teacher"),
        )
        return True

    if step == "ai_chat":
        if value.lower() in {"/menu", "меню", "выйти", "⬅️ завершить диалог"}:
            await _set_state(user, None)
            ai_psychologist.reset_session(max_id)
            await _send(
                chat_id,
                "Диалог завершён. Я всегда на связи 🤍",
                menu_keyboard("student"),
            )
            return True
        score, _themes = mvp.analyze_stress(value)
        if score >= 5:
            await _send(
                chat_id,
                AI_CRISIS_REPLY,
                keyboard(
                    (("💬 Да, передать психологу", "stress:yes"),),
                    (("🤝 Хочу поговорить вживую", "stress:live"),),
                    (("⬅️ Завершить диалог", "student:ai_chat_exit"),),
                ),
            )
            return True
        await _typing(chat_id)
        reply = await ai_psychologist.ask(value, max_id)
        reply, needs_human = ai_psychologist.pop_human_marker(reply)
        if score >= 4 or needs_human:
            if score >= 4:
                reply += (
                    "\n\nЕсли станет совсем тяжело — я могу анонимно передать сигнал "
                    "школьному психологу, только с твоего согласия."
                )
            else:
                reply += (
                    "\n\nЧувствую, что тебе сейчас нужен живой специалист. "
                    "Это не страшно и не стыдно — я помогу связаться с психологом, "
                    "всё конфиденциально."
                )
            await _send(
                chat_id,
                reply,
                keyboard(
                    (("💬 Передать психологу", "stress:yes"),),
                    (("🤝 Поговорить с живым психологом", "stress:live"),),
                    (("⬅️ Завершить диалог", "student:ai_chat_exit"),),
                ),
            )
            return True
        await _send(
            chat_id,
            reply,
            keyboard((("⬅️ Завершить диалог", "student:ai_chat_exit"),)),
        )
        return True

    return False


async def _create_homework(
    chat_id: int | None, user: User, state: dict[str, Any], points: int
) -> None:
    """Сохранить домашку учителя (общий финал для ввода баллов и кнопок-баллов)."""
    due = state["due"]
    class_name = state.get("class_name")
    item = await mvp.create_homework(
        user.id,
        user.id,
        state["subject"],
        state["description"],
        due,
        class_name,
        points=points,
    )
    await _set_state(user, None)
    target = f" для {class_name}" if class_name else ""
    await _send(
        chat_id,
        f"✅ Домашка{target} добавлена:\n{item.subject} — {item.description}\n"
        f"Срок: {due:%d.%m.%Y %H:%M}\nБаллы за сдачу: {item.points}",
        keyboard(
            (("✅ Сдача домашки", "teacher:hw_mark"),),
            (("📝 Домашка", "teacher:homework"), ("⬅️ В меню", "menu:teacher")),
        ),
    )



async def _request_link(chat_id: int | None, parent: User, student: User) -> None:
    link, created = await crud.request_parent_student_link(parent.id, student.id)
    if not created:
        if link.status == "approved":
            await _send(chat_id, f"{student.name} уже привязан к вашему кабинету.")
        else:
            await _send(chat_id, f"Запрос ученику {student.name} уже ожидает подтверждения.")
        return
    await _send(chat_id, f"Запрос на связь отправлен ученику {student.name}. Ожидайте подтверждения.")
    await _send_link_request(link.id, parent, student)


def _parse_datetime(value: str) -> datetime:
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(value.strip(), pattern)
            if pattern == "%d.%m.%Y":
                return parsed.replace(hour=23, minute=59)
            return parsed
        except ValueError:
            continue
    raise ValueError("Дата должна быть в формате ДД.ММ.ГГГГ [ЧЧ:ММ].")


def _day_number(value: str) -> int:
    names = {
        "пн": 1,
        "понедельник": 1,
        "вт": 2,
        "вторник": 2,
        "ср": 3,
        "среда": 3,
        "чт": 4,
        "четверг": 4,
        "пт": 5,
        "пятница": 5,
        "сб": 6,
        "суббота": 6,
        "вс": 7,
        "воскресенье": 7,
    }
    try:
        return names[value.strip().casefold()]
    except KeyError as error:
        raise ValueError("Укажите день недели (пн–вс).") from error


WEEKDAY_LABELS = {
    1: "Понедельник",
    2: "Вторник",
    3: "Среда",
    4: "Четверг",
    5: "Пятница",
    6: "Суббота",
    7: "Воскресенье",
}
WEEKDAY_SHORT = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
AI_CRISIS_REPLY = (
    "То, о чём ты пишешь, звучит очень серьёзно. Ты не один(на) — рядом есть люди, "
    "готовые помочь.\n"
    "Если чувствуешь, что тебе угрожает опасность, — позвони 103 (скорая помощь) "
    "или на бесплатный анонимный телефон доверия 8-800-2000-122.\n"
    "Хочешь, я передам школьному психологу, что тебе нужна поддержка? "
    "Он не увидит нашу переписку — только сам факт обращения."
)
BREATHING_PRACTICE = (
    "🌬 Дыхательная практика «Квадрат»\n"
    "Сядь удобно и расслабь плечи.\n"
    "1. Вдох носом — 4 секунды\n"
    "2. Задержка дыхания — 4 секунды\n"
    "3. Медленный выдох ртом — 4 секунды\n"
    "4. Задержка — 4 секунды\n"
    "Повтори 4–6 раз. Если такой ритм тяжёлый — делай паузы короче."
)
STRESS_CARDS: tuple[tuple[str, str], ...] = (
    (
        "🃏 Карточка «5-4-3-2-1» (2 минуты)",
        "Остановись и назови про себя:\n"
        "• 5 вещей, которые видишь;\n"
        "• 4 вещи, которые можешь потрогать;\n"
        "• 3 звука, которые слышишь;\n"
        "• 2 запаха;\n"
        "• 1 вещь, за которую благодарен(на) сегодня.\n"
        "Упражнение возвращает в «здесь и сейчас» и снижает тревогу.",
    ),
    (
        "🃏 Карточка «Минута покоя» (1 минута)",
        "Поставь таймер на 1 минуту, закрой глаза и просто дыши.\n"
        "Считай про себя: вдох — 4 счёта, выдох — 6 счетов.\n"
        "Если мысли убегают — мягко вернись к счёту. Это нормально.",
    ),
    (
        "🃏 Карточка «Плечи вниз» (1 минута)",
        "Сядь ровно. На вдохе подними плечи к ушам и сожми кулаки.\n"
        "На выдохе резко опусти плечи и разожми кулаки.\n"
        "Повтори 5 раз — тело расслабится, а с ним и мысли.",
    ),
    (
        "🃏 Карточка «Холодная вода» (1 минута)",
        "Умой лицо прохладной водой или подержи руки под холодной струёй 30 секунд.\n"
        "Холод переключает нервную систему и помогает сбить сильную тревогу.",
    ),
    (
        "🃏 Карточка «Три благодарности» (2 минуты)",
        "Назови вслух или запиши три вещи, которые сегодня получились или порадовали.\n"
        "Даже мелочи: вкусный чай, удачная шутка, солнце из окна.\n"
        "Это переключает мозг с тревоги на опору.",
    ),
    (
        "🃏 Карточка «Потягушки» (1 минута)",
        "Встань и потянись руками вверх, глубоко вдохнув.\n"
        "На выдохе наклонись вперёд и расслабь спину.\n"
        "Повтори 3 раза: движение снимает зажимы от долгого сидения.",
    ),
)
# Редкие карточки: открываются наградой «🃏 Пак редких антистресс-карточек».
RARE_STRESS_CARDS: tuple[tuple[str, str], ...] = (
    (
        "🃏 Карточка «Северное сияние» (редкая)",
        "Закрой глаза и представь небо над севером: зелёные волны света "
        "медленно плывут по нему.\nДыши в ритме волн — вдох на подъёме, "
        "выдох на спаде. Полминуты такой визуализации снижает тревогу.",
    ),
    (
        "🃏 Карточка «Письмо себе» (редкая)",
        "Напиши себе три строки поддержки, как лучшему другу: "
        "«Я горжусь тобой за…», «Тебе сейчас трудно, потому что…», "
        "«Завтра я помогу тебе…».\nЧтение собственного письма успокаивает "
        "сильнее чужих советов.",
    ),
    (
        "🃏 Карточка «Пять звуков» (редкая)",
        "Остановись и найди вокруг пять звуков, которые обычно не замечаешь: "
        "гул лампы, тиканье, ветер за окном.\nНазови их про себя по очереди — "
        "это упражнение заземляет и возвращает в момент «здесь и сейчас».",
    ),
)
# Значок профиля для каждой награды. Набор совпадает с mvp.REWARD_CATALOG:
# лишние «плюшки» (мелкие значки и титулы) убраны, оставлены только крутые.
REWARD_PROFILE_EMOJI = {
    "title_captain": "⚓",
    "title_hero": "🦸",
    "title_champion": "🏆",
    "title_genius": "🧠",
    "frame_gold": "🖼",
    "emoji_cosmos": "🚀",
    "points_boost_3": "🚀",
    "points_boost": "⚡",
    "daily_spin": "🎰",
    "theme_sunset": "🌇",
    "card_pack": "🃏",
}
# Экстренные контакты: показываются ученику вместе с подтверждением сигнала.
CRISIS_CONTACTS = (
    "🆘 Экстренная помощь\n"
    "Если тебе угрожает опасность или становится совсем тяжело — позвони:\n"
    "• 103 — скорая помощь;\n"
    "• 8-800-2000-122 — бесплатный анонимный телефон доверия для детей "
    "и подростков.\n"
    "Попросить о помощи — нормально и не стыдно."
)
# Кнопка «⚙️ Настройки» есть в каждом кабинете — возвращаем в свой.
SETTINGS_PAYLOAD_BY_ROLE = {
    "student": "student:settings",
    "teacher": "teacher:settings",
    "parent": "parent:settings",
    "psychologist": "psychologist:settings",
}


def _short_school(name: object, limit: int = 28) -> str:
    """Название школы для кнопки: MAX ограничивает текст кнопки."""
    cleaned = " ".join(str(name or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _points_word(amount: int) -> str:
    """Правильная форма слова «очко» для числа (1 очко, 3 очка, 6 очков)."""
    rest100 = abs(int(amount)) % 100
    rest10 = rest100 % 10
    if rest100 in range(11, 15) or rest10 == 0 or rest10 in range(5, 10):
        return "очков"
    if rest10 == 1:
        return "очко"
    return "очка"


async def _mood_gain_safe(user_id: Any) -> int:
    """Сколько очков даст отметка настроения (без падения, если БД недоступна)."""
    try:
        return await mvp.mood_gain(int(user_id))
    except Exception:  # noqa: BLE001 - текст сообщения важнее точной цифры
        logger.exception("Could not compute mood points for user %s", user_id)
        return mvp.MOOD_POINTS


def _weekday_label(day: int) -> str:
    return WEEKDAY_LABELS.get(day, "День")


def _parse_lesson_time(value: str) -> time:
    match = re.fullmatch(r"(\d{1,2})[:.\s](\d{2})", value.strip())
    if match is None:
        raise ValueError("Время должно быть в формате ЧЧ:ММ, например 08:30.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Время должно быть в формате ЧЧ:ММ, например 08:30.")
    return time(hour=hour, minute=minute)


def _days_left(due: datetime) -> str:
    delta = due - datetime.now()
    if delta.total_seconds() <= 0:
        return "просрочено"
    if delta.days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            return f"осталось {max(delta.seconds // 60, 1)} мин"
        return f"осталось {hours} ч"
    if delta.days == 1:
        return "остался 1 день"
    if delta.days < 5:
        return f"осталось {delta.days} дня"
    return f"осталось {delta.days} дней"


def _weekday_rows(prefix: str) -> tuple[tuple[tuple[str, str], ...], ...]:
    items = tuple((WEEKDAY_SHORT[day], f"{prefix}:{day}") for day in range(1, 8))
    return tuple(tuple(items[i : i + 4]) for i in range(0, len(items), 4))


def _weekday_keyboard(prefix: str) -> Any:
    return keyboard(*_weekday_rows(prefix))


def _render_schedule(items: list[Any], day: int, class_name: str | None = None) -> str:
    header = (
        f"📚 Расписание {class_name} класса, {_weekday_label(day)}:"
        if class_name
        else f"📚 {_weekday_label(day)}:"
    )
    if not items:
        return f"{header} уроков нет."
    lines = [header]
    for item in items:
        room = f", каб. {item.room}" if item.room else ""
        lines.append(f"  {item.time:%H:%M} — {item.subject}{room}")
    return "\n".join(lines)


def _render_homework(pairs: list[tuple[Any, bool]]) -> str:
    if not pairs:
        return "📝 Домашка: активных заданий нет 🎉"
    lines = ["📝 Домашка:"]
    for item, done in pairs:
        mark = "✅" if done else "⬜"
        lines.append(
            f"{mark} {item.subject} — {item.description}\n"
            f"    срок: {item.deadline:%d.%m.%Y %H:%M} ({_days_left(item.deadline)})\n"
            f"    баллы за сдачу: {getattr(item, 'points', 10)}"
        )
    return "\n".join(lines)


def _render_deadlines(items: list[Any]) -> str:
    if not items:
        return "⏰ Ближайших контрольных и сроков нет 🎉"
    lines = ["⏰ Дедлайны:"]
    for item in items:
        lines.append(
            f"  • {item.title} ({item.kind}) — {item.date:%d.%m.%Y %H:%M}, "
            f"{_days_left(item.date)}"
        )
    return "\n".join(lines)


def _render_announcements(items: list[Any]) -> str:
    if not items:
        return "📢 Объявлений класса пока нет."
    lines = ["📢 Объявления класса:"]
    for item in items:
        lines.append(f"  • {item.created_at:%d.%m.%Y} — {item.text}")
    return "\n".join(lines)


def _render_mood(entries: list[Any]) -> str:
    if not entries:
        return "📖 Дневник настроения пока пуст. Отметь, как ты сегодня — это займёт 5 секунд."
    lines = ["📖 Настроение за последние 14 дней:"]
    for entry in entries:
        lines.append(f"  {entry.date:%d.%m.%Y} — {'🙂' * entry.score}")
    return "\n".join(lines)


async def _ask_owner_class(
    chat_id: int | None, user: User, prefix: str, prompt: str
) -> None:
    classes = await crud.classes_for_teacher(user.id)
    if not classes:
        await _send(
            chat_id,
            "Сначала создайте класс — кнопка «➕ Создать класс».",
            menu_keyboard("teacher"),
        )
        return
    names = [item.name for item in classes]
    rows = tuple(
        tuple((name, f"{prefix}:class:{name}") for name in names[i : i + 3])
        for i in range(0, len(names), 3)
    )
    await _send(chat_id, prompt, keyboard(*rows))


async def _show_student_schedule(chat_id: int | None, user: User, day: int) -> None:
    if not user.class_name:
        await _send(
            chat_id,
            "📚 Расписание ведёт учитель вашего класса.\n"
            "Чтобы его увидеть, войдите в класс — логин и пароль выдаёт учитель.",
            keyboard(
                (("🏫 Войти в класс", "student:join_class"),),
                (("⬅️ В меню", "menu:student"),),
            ),
        )
        return
    items = await mvp.schedule_for_day(user, day)
    today = datetime.now().isoweekday()
    tomorrow = today % 7 + 1
    rows: list[tuple[tuple[str, str], ...]] = [
        (
            ("📌 Сегодня", f"student:schedule:{today}"),
            ("👉 Завтра", f"student:schedule:{tomorrow}"),
            ("📅 Неделя", "student:schedule_week"),
        )
    ]
    rows.extend(_weekday_rows("student:schedule"))
    rows.append((("⬅️ В меню", "menu:student"),))
    await _send(chat_id, _render_schedule(items, day, user.class_name), keyboard(*rows))


async def _show_teacher_class_schedule(
    chat_id: int | None, user: User, class_name: str
) -> None:
    """Расписание класса для учителя: просмотр, добавление и удаление уроков."""
    items = await mvp.schedule_for_class(class_name, user.school)
    lines = [f"🗓 Расписание класса {class_name}:"]
    if not items:
        lines.append("  уроков пока нет — добавьте первый.")
    else:
        current_day = 0
        for item in items:
            if item.day_of_week != current_day:
                current_day = item.day_of_week
                lines.append(f"{_weekday_label(current_day)}:")
            room = f", каб. {item.room}" if item.room else ""
            lines.append(f"  {item.time:%H:%M} — {item.subject}{room}")
    rows: list[tuple[tuple[str, str], ...]] = [
        (("➕ Добавить урок", f"teacher:schedule_add:{class_name}"),)
    ]
    rows.extend(
        (
            (
                f"🗑 {WEEKDAY_SHORT[item.day_of_week]} "
                f"{item.time:%H:%M} {item.subject}",
                f"teacher:lesson_del:{item.id}:{class_name}",
            ),
        )
        for item in items
        if item.created_by == user.id
    )
    rows.append((("⬅️ Классы", "teacher:schedule"), ("⬅️ В меню", "menu:teacher")))
    await _send(chat_id, "\n".join(lines), keyboard(*rows))


async def _show_student_homework(chat_id: int | None, user: User) -> None:
    pairs = await mvp.active_homework(user)
    rows: list[tuple[tuple[str, str], ...]] = [
        (("⬅️ В меню", "menu:student"),),
    ]
    text = _render_homework(pairs)
    if pairs:
        text += "\n\nСдачу отмечает учитель — статус обновится автоматически 👩‍🏫"
    await _send(chat_id, text, keyboard(*rows))


async def _show_teacher_hw_mark(chat_id: int | None, user: User) -> None:
    """Вход в отметку сдачи: список собственных активных заданий учителя."""
    items = await mvp.teacher_assignments(user)
    if not items:
        await _send(
            chat_id,
            "Пока нет активных заданий, которые вы задали. "
            "Создайте домашку кнопкой «📝 Домашка».",
            keyboard((("⬅️ В меню", "menu:teacher"),)),
        )
        return
    lines = ["✅ Отметка сдачи. Выберите задание:"]
    rows: list[tuple[tuple[str, str], ...]] = [
        (
            (
                f"📝 {item.subject} · {item.class_name or '—'} · {getattr(item, 'points', 10)} б.",
                f"teacher:hw_roster:{item.id}",
            ),
        )
        for item in items
    ]
    rows.append((("⬅️ В меню", "menu:teacher"),))
    await _send(chat_id, "\n".join(lines), keyboard(*rows))


async def _show_teacher_hw_roster(
    chat_id: int | None, user: User, homework_id: int
) -> None:
    """Список учеников класса с переключателями сдачи."""
    roster = await mvp.homework_roster(user, homework_id)
    if roster is None:
        await _send(chat_id, "Задание не найдено или вы не его автор.")
        return
    done_count = sum(1 for _student, done in roster if done)
    lines = [f"👥 Кто сдал ({done_count} из {len(roster)}):"]
    if not roster:
        lines.append("В классе пока нет учеников.")
    rows: list[tuple[tuple[str, str], ...]] = [
        (
            (
                f"{'✅' if done else '⬜'} {student.name}",
                f"teacher:hw_toggle:{homework_id}:{student.id}",
            ),
        )
        for student, done in roster
    ]
    rows.append((("⬅️ Задания", "teacher:hw_mark"), ("⬅️ В меню", "menu:teacher")))
    await _send(chat_id, "\n".join(lines), keyboard(*rows))


REWARDS_PER_PAGE = 8


async def _show_student_rewards(
    chat_id: int | None, user: User, page: int = 0
) -> None:
    """Список наград и кастомизация профиля за очки (страницами по 8 штук).

    Каталог большой, а клавиатура MAX ограничена по числу рядов, поэтому
    награды показываются порциями с кнопками «▶️ Ещё» и «⬅️ Предыдущие».
    """
    owned = await mvp.user_reward_keys(user.id)
    catalog = list(mvp.REWARD_CATALOG.items())
    pages = max(1, -(-len(catalog) // REWARDS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    lines = [
        f"🎁 Награды и кастомизация профиля\nТвои очки: {user.points} 🪙",
        f"Страница {page + 1} из {pages}",
    ]
    buy_buttons: list[tuple[str, str]] = []
    for key, (title, cost, description) in catalog[
        page * REWARDS_PER_PAGE : (page + 1) * REWARDS_PER_PAGE
    ]:
        if key in owned:
            lines.append(f"✅ {title} — открыто")
        else:
            lines.append(f"🔒 {title} — {cost} очков\n    {description}")
            buy_buttons.append((f"{title} — {cost} 🪙", f"student:reward_buy:{key}"))
    if not buy_buttons and pages == 1:
        lines.append("Открыты все награды — ты молодец! 🎉")
    rows: list[tuple[tuple[str, str], ...]] = [
        tuple(buy_buttons[index : index + 2])
        for index in range(0, len(buy_buttons), 2)
    ]
    if page + 1 < pages:
        rows.append((("▶️ Ещё награды", f"student:rewards:{page + 1}"),))
    if page > 0:
        rows.append((("⬅️ Предыдущие награды", f"student:rewards:{page - 1}"),))
    if "daily_spin" in owned:
        rows.append((("🎰 Крутить колесо удачи", "student:spin"),))
    rows.append(
        (("⬅️ В профиль", "student:profile"), ("⬅️ В меню", "menu:student"))
    )
    await _send(chat_id, "\n".join(lines), keyboard(*rows))


async def _notify_parents_about_deadline(
    class_name: str, teacher: User, item: Any, kind: str, due: datetime
) -> None:
    """Учитель добавил контрольную → родители получают уведомление ."""
    try:
        parents = await mvp.parents_of_class_students(class_name, teacher.school_name)
    except Exception:
        logger.exception("Could not find parents of class %s", class_name)
        return
    for parent in parents:
        try:
            await _send(
                None,
                f"🔔 В классе {class_name} добавлено событие: "
                f"«{item.title}» ({kind}) — {due:%d.%m.%Y %H:%M}.",
                keyboard(
                    (("🔔 Уведомления", "parent:notifications"),),
                    (("⬅️ В меню", "menu:parent"),),
                ),
                user_id=int(parent.max_user_id),
            )
        except Exception:
            logger.exception(
                "Could not notify parent %s about deadline", parent.max_user_id
            )


async def _save_survey_hour(
    chat_id: int | None, max_id: str, user: User, hour: int
) -> None:
    await mvp.update_profile(max_id, mood_survey_hour=hour)
    await _set_state(user, None)
    await _send(
        chat_id,
        f"⏰ Опрос «Как ты сегодня?» будет приходить в {hour}:00.",
        keyboard(
            (("⚙️ Настройки", "student:settings"),),
            (("⬅️ В меню", "menu:student"),),
        ),
    )


async def _show_reminder_settings(chat_id: int | None, user: User) -> None:
    """Панель напоминаний: общий выключатель и сроки (неделя / 3 дня / день)."""

    def mark(flag: bool) -> str:
        return "✅ вкл" if flag else "❌ выкл"

    all_on = bool(user.reminders_enabled)
    n7 = bool(getattr(user, "notify_7d", True))
    n3 = bool(getattr(user, "notify_3d", True))
    n1 = bool(getattr(user, "notify_1d", True))
    await _send(
        chat_id,
        "🔔 Настройки напоминаний\n"
        f"Все уведомления: {mark(all_on)}\n"
        f"📆 За неделю до срока: {mark(n7)}\n"
        f"📅 За 3 дня до срока: {mark(n3)}\n"
        f"⏰ За день до срока: {mark(n1)}\n\n"
        "Напоминания приходят о домашке и дедлайнах (контрольные, проекты, "
        "зачёты), которые задаёт учитель.",
        keyboard(
            (
                (
                    "🔕 Выключить все" if all_on else "🔔 Включить все",
                    "settings:reminders:all",
                ),
            ),
            ((f"📆 За неделю: {mark(n7)}", "settings:reminders:7d"),),
            ((f"📅 За 3 дня: {mark(n3)}", "settings:reminders:3d"),),
            ((f"⏰ За день: {mark(n1)}", "settings:reminders:1d"),),
            (("⬅️ В меню", f"menu:{_role_value(user.role)}"),),
        ),
    )


async def _show_dev_panel(chat_id: int | None, user: User | None) -> None:
    """Скрытая панель команды разработчиков: любая роль в любой момент."""
    if user is None:
        await _send(
            chat_id,
            "Профиль не найден. Сначала отправьте /start, затем снова откройте панель.",
        )
        return
    role = _role_value(user.role)
    lines = [
        "🛠 Панель разработчика (скрытый режим)",
        f"ID: {user.max_user_id}",
        f"ФИО: {user.full_name}",
        f"Роль: {DEV_ROLE_TITLES.get(role, role)}",
    ]
    if user.school_name:
        lines.append(f"Школа: {user.school_name}")
    if user.class_name:
        lines.append(f"Класс: {user.class_name}")
    impersonated = _impersonation.get(str(user.max_user_id))
    if impersonated:
        lines.append(f"🎭 Сейчас работаете как тестовый аккаунт (MAX ID {impersonated})")
    lines.append("")
    lines.append("Нажми на роль — переключение сразу, без повторной регистрации:")
    rows: list[tuple[tuple[str, str], ...]] = []
    for value, title in DEV_ROLE_TITLES.items():
        mark = "✅ " if value == role else ""
        rows.append(((f"{mark}{title}", f"dev:role:{value}"),))
    rows.append((("♻️ Сбросить роль (выбрать заново)", "dev:reset"),))
    rows.append((("📋 Меню по роли", "dev:menu"),))
    rows.append((("👥 Кто в системе", "dev:directory"),))
    rows.append((("🧪 Тестовый сигнал психологу", "dev:test_signal"),))
    if impersonated:
        rows.append((("🎭 Выйти из тестового ученика", "dev:impersonate_exit"),))
    else:
        rows.append((("🎭 Войти как тестовый ученик", "dev:impersonate"),))
    await _send(chat_id, "\n".join(lines), keyboard(*rows))


async def _show_dev_directory(chat_id: int | None) -> None:
    """Кто зарегистрирован в системе: роли и школы.

    Без этого невозможно понять, почему сигнал не доходит до психолога:
    например, когда в базе просто нет ученика или психолог не зарегистрирован.
    """
    users = await mvp.list_directory()
    if not users:
        await _send(chat_id, "В системе пока нет ни одного аккаунта.")
        return
    lines = [f"👥 Аккаунты в системе: {len(users)}"]
    for item in users:
        role = _role_value(item.role)
        lines.append(
            f"• {DEV_ROLE_TITLES.get(role, role)} — {item.full_name}\n"
            f"   MAX ID: {item.max_user_id} | школа: {item.school_name or '—'}"
            f" | класс: {item.class_name or '—'}"
        )
    psychologists = [
        item for item in users if _role_value(item.role) == "psychologist"
    ]
    if not psychologists:
        lines.append(
            "\n⚠️ Психологов нет: ученику некому передать сигнал. Переключите "
            "роль на «🧠 Психолог» и укажите школу — или зарегистрируйте второй "
            "аккаунт (один MAX ID = одна роль)."
        )
    await _send(
        chat_id,
        "\n".join(lines),
        keyboard(
            (("🛠 Панель разработчика", "dev:menu_panel"),),
            (("⬅️ В меню", f"menu:{_role_value(users[0].role)}"),),
        ),
    )


async def _send_dev_test_signal(chat_id: int | None, user: User) -> None:
    """Демо-сигнал в кабинет психолога: проверяет маршрутизацию без второго аккаунта."""
    if _role_value(user.role) != "psychologist":
        await _send(
            chat_id,
            "Тестовый сигнал создаётся для кабинета психолога.\n"
            "Сначала переключите роль на «🧠 Психолог» и укажите школу.",
            keyboard((("🛠 Панель разработчика", "dev:menu_panel"),)),
        )
        return
    demo = await mvp.ensure_demo_student(
        school_name=user.school_name or "Школа1",
        class_name="9А",
    )
    signal = await mvp.create_stress_signal(
        demo.id,
        user.id,
        risk_level=4,
        themes=["демо-сигнал", "тревога перед экзаменом"],
        preferred_format="chat",
    )
    await _send(
        chat_id,
        f"🧪 Тестовый сигнал №{signal.id} создан от «{demo.full_name}» "
        f"(школа: {demo.school_name}, класс: {demo.class_name}).\n"
        "Откройте кабинет психолога — сигнал будет в списке «📥 Сигналы».",
        keyboard(
            (("📥 Сигналы", "psychologist:signals"),),
            (("🛠 Панель разработчика", "dev:menu_panel"),),
        ),
    )


async def _handle_dev_payload(chat_id: int | None, max_id: str, payload: str) -> None:
    """Обработка нажатий скрытой dev-панели (только для config.DEV_MAX_IDS)."""
    user = await mvp.user_by_max_id(max_id)
    if user is None:
        await _show_dev_panel(chat_id, None)
        return
    if payload.startswith("dev:role:"):
        role = payload.rsplit(":", 1)[-1]
        if role in ROLE_LABELS:
            await _set_state(user, None)
            values: dict[str, Any] = {"role": role, "status": "approved"}
            if role == "student" and not user.class_name:
                school_class = await mvp.first_school_class()
                if school_class is not None:
                    values["class_name"] = school_class.name
            user = await mvp.dev_force_profile(max_id, **values)
    elif payload == "dev:reset":
        await _set_state(user, None)
        user = await mvp.dev_force_profile(
            max_id, role="unselected", status="approved"
        )
    elif payload == "dev:menu":
        if _role_value(user.role) == "unselected":
            await _send(chat_id, "Роль не выбрана — выберите её в панели.")
        else:
            await _menu(chat_id, user, int(max_id))
        return
    elif payload == "dev:directory":
        await _show_dev_directory(chat_id)
        return
    elif payload == "dev:test_signal":
        await _send_dev_test_signal(chat_id, user)
        return
    elif payload == "dev:menu_panel":
        await _show_dev_panel(chat_id, user)
        return
    elif payload == "dev:impersonate":
        # Один MAX ID = одна роль, поэтому путь «ученик пишет психологу»
        # проверяем от имени служебного ученика, не выходя из своего чата.
        school = (user.school_name or "").strip()
        if not school:
            # Школу берём такую, где психолог точно есть, — иначе сигнал не дойдёт.
            schools = await mvp.psychologist_schools()
            school = schools[0] if schools else "Школа1"
        demo = await mvp.ensure_demo_student(school_name=school, class_name="9А")
        _impersonation[str(user.max_user_id)] = str(demo.max_user_id)
        await _send(
            chat_id,
            f"🎭 Вы вошли как «{demo.full_name}» — ученик, школа "
            f"{demo.school_name}, класс {demo.class_name}.\n"
            "Все кнопки теперь работают от его имени: можно пройти путь ученика "
            "и отправить сигнал психологу (вашему основному аккаунту).\n"
            "Команда /dev по-прежнему открывает панель — там же кнопка выхода.",
            keyboard(
                (("🎓 Кабинет ученика", "menu:student"),),
                (("🧘 Антистресс", "student:stress"),),
                (("🛠 Панель разработчика", "dev:menu_panel"),),
            ),
        )
        return
    elif payload == "dev:impersonate_exit":
        _impersonation.pop(str(user.max_user_id), None)
        await _set_state(user, None)
        await _send(
            chat_id,
            "🎭 Вы вернулись в свой аккаунт.",
            _restart_keyboard(user),
        )
        return
    await _show_dev_panel(chat_id, user)


async def _deliver_signal(
    chat_id: int | None, user: User, psychologist: User, action: str
) -> None:
    """Создать сигнал, подтвердить ученику и сразу уведомить психолога."""
    if await mvp.has_open_signal(user.id, psychologist.id):
        # Сигнал уже в работе — не плодим дубликаты в очереди психолога.
        await _set_state(user, None)
        await _send(
            chat_id,
            "✅ Твой сигнал уже у психолога — он его увидит в кабинете и "
            "свяжется с тобой. Повторно отправлять не нужно.\n\n"
            + CRISIS_CONTACTS,
            keyboard(
                (("🧘 Антистресс", "student:stress"),),
                (("⬅️ В меню", "menu:student"),),
            ),
        )
        return
    risk_level, themes = await mvp.stress_snapshot(user)
    signal = await mvp.create_stress_signal(
        user.id,
        psychologist.id,
        risk_level=risk_level,
        themes=themes,
        preferred_format=action,
        consent=True,
    )
    await _set_state(user, None)
    format_label = "личная встреча" if action == "live" else "переписка"
    await _send(
        chat_id,
        "✅ Сигнал передан психологу — он увидит его в кабинете «📥 Сигналы» "
        f"и свяжется с тобой (формат: {format_label}).\n"
        "Нашу переписку он не видит.\n\n" + CRISIS_CONTACTS,
        keyboard(
            (("🧘 Антистресс", "student:stress"),),
            (("⬅️ В меню", "menu:student"),),
        ),
    )
    # Психолог получает уведомление сразу, а не только когда откроет кабинет.
    try:
        await _send(
            None,
            f"📥 Новый сигнал №{signal.id} (риск {signal.risk_level}/5)\n"
            f"Ученик: {user.name}, класс {user.class_name or '—'}, "
            f"школа: {user.school_name or '—'}\n"
            f"Темы: {signal.themes or 'не указаны'}\n"
            f"Формат связи: {format_label}",
            keyboard(
                (("📥 Открыть сигналы", "psychologist:signals"),),
                (("⬅️ В меню", "menu:psychologist"),),
            ),
            user_id=int(psychologist.max_user_id),
        )
    except Exception:  # noqa: BLE001 - уведомление не должно ломать сигнал
        logger.exception(
            "Could not notify psychologist %s about signal %s",
            psychologist.max_user_id,
            signal.id,
        )


async def _ask_psychologist_school(
    chat_id: int | None, user: User, action: str, reason: str
) -> None:
    """Выбор школы кнопками — так сигнал гарантированно дойдёт до психолога.

    Школы берутся только те, где есть одобренный психолог, поэтому ученик не
    может выбрать «пустую» школу и остаться без помощи. Ручной ввод сохранён
    как запасной вариант (например, если психолог ещё не подтверждён админом).
    """
    schools = await mvp.psychologist_schools()
    await _set_state(user, "stress_school", stress_format=action, schools=schools)
    rows: list[tuple[tuple[str, str], ...]] = []
    for start in range(0, len(schools), 2):
        rows.append(
            tuple(
                (_short_school(name), f"stress:school:{index}")
                for index, name in enumerate(schools[start : start + 2], start=start)
            )
        )
    rows.append((("✍️ Ввести название школы", "stress:school_manual"),))
    rows.append((("🧑‍⚕️ Дежурному психологу", "stress:any"),))
    rows.append((("🆘 Экстренная помощь", "stress:help"),))
    rows.append((("⬅️ В меню", "menu:student"),))
    await _send(
        chat_id,
        reason + "\n\nВыбери школу из списка — там психолог точно есть:",
        keyboard(*rows),
    )


async def _start_stress_signal(chat_id: int | None, user: User, action: str) -> None:
    """Создать сигнал психологу школы без тупиков.

    Возможны три ситуации, и ни одна не должна оставлять ученика без помощи:
    школа не указана (предлагаем выбор кнопками), школа указана, но психолога
    там нет (снова выбор + дежурный психолог), психолог найден (создаём сигнал).
    """
    if not (user.school_name or "").strip():
        await _ask_psychologist_school(
            chat_id,
            user,
            action,
            "Чтобы найти психолога твоей школы, укажи её.",
        )
        return
    psychologist = await mvp.psychologist_for_school(user.school_name)
    if psychologist is None:
        schools = await mvp.psychologist_schools()
        reason = f"В школе «{user.school_name}» психолог пока не зарегистрирован."
        if not schools:
            await _send(
                chat_id,
                reason + "\n\n"
                "Пока в системе нет ни одного психолога, поэтому передать сигнал "
                "некому.\n"
                "Что это значит: школьный психолог должен зарегистрироваться в "
                "боте («👩‍🏫 Я сотрудник школы» → «🧠 Психолог») и указать ту же "
                "школу — сразу после этого кнопка заработает.\n\n"
                "Поддержка доступна прямо сейчас: ИИ-психолог на связи 24/7, "
                "а при острой ситуации помогут по телефонам ниже.\n\n"
                + CRISIS_CONTACTS,
                keyboard(
                    (("🤖 Чат с ИИ-психологом", "student:ai_chat"),),
                    (("✍️ Указать другую школу", "stress:school_manual"),),
                    (("🧘 Антистресс", "student:stress"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        await _ask_psychologist_school(chat_id, user, action, reason)
        return
    await _deliver_signal(chat_id, user, psychologist, action)


async def _handle_message_created(event: Any) -> None:
    real_id = _max_user_id(event)
    chat_id = _chat_id(event)
    text = getattr(getattr(getattr(event, "message", None), "body", None), "text", None)
    if real_id is None or chat_id is None or not text:
        return
    text = text.strip()
    max_id = real_id

    try:
        _real_id_ctx.set(real_id)
        # В dev-режиме «войти как» события играем от тестового аккаунта.
        max_id = _effective_max_id(real_id)
        user = await _ensure_approved(await mvp.user_by_max_id(max_id))
        role_value = _role_value(user.role) if user is not None else None
        _role_ctx.set(role_value if role_value in ROLE_LABELS else None)
        # Скрытая dev-панель: у посторонних команда даёт тот же ответ,
        # что и любой неизвестный текст, — никаких следов её существования.
        if text == config.DEV_COMMAND and _is_dev(real_id):
            real_user = (
                user if max_id == real_id else await mvp.user_by_max_id(real_id)
            )
            await _show_dev_panel(chat_id, real_user)
            return
        if text.split(maxsplit=1)[0].split("@", 1)[0] == "/start":
            await _set_state(User(max_user_id=max_id, role=UserRole.UNSELECTED), None)
            if user is None:
                await _set_state(
                    User(max_user_id=max_id, role=UserRole.UNSELECTED),
                    "register_name",
                )
                await _send(
                    chat_id,
                    "Привет! \ud83d\udc4b\n"
                    "Я — бот «Школьный штаб». Здесь ученики, родители и учителя могут "
                    "следить за расписанием, домашкой и дедлайнами.\n\n"
                    "Чтобы начать, напиши, как тебя зовут (ФИО).",
                )
                return
            if _missing_full_name(user) and user.role in {
                UserRole.TEACHER,
                UserRole.PSYCHOLOGIST,
            }:
                await _set_state(user, "onboarding_staff_name")
                await _send(
                    chat_id,
                    "В профиле сотрудника не сохранено ФИО. Пожалуйста, введите ФИО.",
                )
                return
            if user.role == UserRole.UNSELECTED:
                await _send(
                    chat_id,
                    "Выбери, кто ты в школе \ud83d\udc47\n(после выбора роль изменить не получится)",
                    registration_roles_keyboard(),
                )
                return
            await _menu(chat_id, user, int(max_id))
            return

        state = _get_state(max_id)
        if state is not None:
            if user is None:
                user = User(max_user_id=max_id, role=UserRole.UNSELECTED)
            handled = await _handle_input_step(
                chat_id,
                int(max_id),
                user,
                state["step"],
                text,
                state,
            )
            if handled:
                return

        if user is None:
            await _set_state(
                User(max_user_id=max_id, role=UserRole.UNSELECTED),
                "register_name",
            )
            await _send(
                chat_id,
                "Привет! \ud83d\udc4b\n"
                "Я — бот «Школьный штаб». Здесь ученики, родители и учителя могут "
                "следить за расписанием, домашкой и дедлайнами.\n\n"
                "Чтобы начать, напиши, как тебя зовут (ФИО).",
            )
        elif user.role == UserRole.UNSELECTED:
            await _send(
                chat_id,
                "Выбери, кто ты в школе \ud83d\udc47\n(после выбора роль изменить не получится)",
                registration_roles_keyboard(),
            )
        elif user.role == UserRole.STUDENT:
            score, themes, _ = await mvp.record_chat_message(user, text)
            threshold = await mvp.check_stress_threshold(user)
            if threshold:
                await _set_state(user, "stress_offer")
                await _send(
                    chat_id,
                    "Я вижу, что последние дни тебе тяжело.\n"
                    "Это нормально — иногда всем бывает сложно.\n"
                    "Хочешь, я передам школьному психологу, что ты не против поговорить?\n"
                    "Он не увидит наши сообщения — только то, что тебе нужна поддержка.",
                    keyboard(
                        (("💬 Да, передать психологу", "stress:yes"),),
                        (("🤝 Хочу поговорить вживую", "stress:live"),),
                        (("✍️ Хочу переписываться", "stress:chat"),),
                        (("⏳ Позже", "stress:later"), ("❌ Нет, спасибо", "stress:no")),
                    ),
                )
            else:
                await _send(
                    chat_id,
                    "Я тебя записал. Если нужна помощь — используй кнопки ниже.",
                    _restart_keyboard(user),
                )
        else:
            await _send(
                chat_id,
                "Используйте кнопки меню.",
                _restart_keyboard(user),
            )
    except Exception:
        logger.exception("Failed to handle MAX message from user %s", max_id)
        await _send(
            chat_id,
            "Не удалось обработать запрос. Попробуйте еще раз позже.",
            keyboard((("↩️ Начать заново", "onboard:restart"),)),
        )


async def _handle_callback(event: Any) -> None:
    callback = getattr(event, "callback", None)
    payload = getattr(callback, "payload", None)
    real_id = _max_user_id(event)
    chat_id = _chat_id(event)
    callback_bot = getattr(event, "bot", None) or bot
    callback_id = getattr(callback, "callback_id", None)
    message = getattr(event, "message", None)
    can_edit_callback_message = (
        callback_id is not None
        and getattr(message, "body", None) is not None
        and callable(getattr(event, "edit", None))
    )
    is_link_decision = bool(
        payload
        and (payload.startswith("link_approve:") or payload.startswith("link_reject:"))
    )
    callback_acknowledged = False

    async def acknowledge_callback() -> None:
        nonlocal callback_acknowledged
        if callback_id and not callback_acknowledged:
            callback_acknowledged = True
            try:
                await callback_bot.send_callback(
                    callback_id=callback_id,
                    notification="Запрос принят.",
                )
            except Exception:
                logger.exception("Could not acknowledge MAX callback %s", callback_id)

    if not (is_link_decision and can_edit_callback_message):
        await acknowledge_callback()
    if not payload or real_id is None:
        await acknowledge_callback()
        return
    max_id = real_id

    try:
        _real_id_ctx.set(real_id)
        # В dev-режиме «войти как» нажатия играем от тестового аккаунта.
        max_id = _effective_max_id(real_id)
        user = await _ensure_approved(await mvp.user_by_max_id(max_id))
        role_value = _role_value(user.role) if user is not None else None
        _role_ctx.set(role_value if role_value in ROLE_LABELS else None)
        # Скрытая dev-панель: чужие dev-payload игнорируются молча.
        # Проверяем настоящий ID — иначе из тестового аккаунта нельзя выйти.
        if payload.startswith("dev:"):
            if _is_dev(real_id):
                await _handle_dev_payload(chat_id, real_id, payload)
            return
        if payload == "onboard:restart":
            # Универсальный выход из любого тупика: работает вместо /start.
            await _set_state(
                user if user is not None else User(max_user_id=max_id),
                None,
            )
            if user is None or user.role == UserRole.UNSELECTED:
                await _send(
                    chat_id,
                    "Выбери, кто ты в школе 👇\n(после выбора роль изменить не получится)",
                    registration_roles_keyboard(),
                )
            else:
                await _menu(chat_id, user, int(max_id))
            return
        if payload == "onboard:role:staff":
            if user is not None and user.role != UserRole.UNSELECTED:
                await _menu(chat_id, user, int(max_id))
                return
            await _send(
                chat_id,
                "Уточните вашу роль 👇",
                staff_roles_keyboard(),
            )
            return
        if payload == "onboard:role_back":
            await _send(
                chat_id,
                "Выбери, кто ты в школе \ud83d\udc47\n(после выбора роль изменить не получится)",
                registration_roles_keyboard(),
            )
            return
        if payload.startswith("onboard:role:"):
            role = payload.rsplit(":", 1)[-1]
            if role not in ROLE_LABELS:
                await _send(
                    chat_id,
                    "Неизвестная роль.",
                    keyboard((("↩️ Выбрать роль", "onboard:restart"),)),
                )
                return
            if user is not None and user.role != UserRole.UNSELECTED:
                await _menu(chat_id, user, int(max_id))
                return
            # Администратора нет: аккаунт активен сразу после выбора роли.
            user = await mvp.update_profile(max_id, role=role, status="approved")
            employee = role in {"teacher", "psychologist"}
            if role == "student":
                await _set_state(user, "onboarding_classes")
                await _send(
                    chat_id,
                    "В каком ты классе? \ud83c\udfeb\n"
                    "Сначала выбери номер кнопкой, а потом напиши букву, например 9А.",
                    grade_keyboard(),
                )
            elif employee:
                if _missing_full_name(user):
                    await _set_state(user, "onboarding_staff_name")
                    await _send(chat_id, "Укажи ФИО — оно появится в профиле.")
                else:
                    await _set_state(
                        user,
                        "onboarding_psychologist_school"
                        if role == "psychologist"
                        else "onboarding_school",
                    )
                    await _send(chat_id, "В какой школе работаешь?")
            else:
                await _set_state(user, "onboarding_parent_child")
                await _send(
                    chat_id,
                    "Введи числовой MAX ID ребёнка. Также можно указать точное ФИО "
                    "(MAX ID можно посмотреть в профиле ученика).",
                )
            return

        if payload.startswith("onboard:grade:"):
            if (
                user is None
                or user.role != UserRole.STUDENT
                or user.status != RequestStatus.APPROVED
            ):
                await _send(
                    chat_id,
                    "Выбор класса доступен только зарегистрированному ученику.",
                    _restart_keyboard(user),
                )
                return
            try:
                grade = int(payload.rsplit(":", 1)[-1])
            except ValueError:
                await _send(
                    chat_id,
                    "Некорректно указан класс.",
                    grade_keyboard(),
                )
                return
            if grade not in range(1, 12):
                await _send(
                    chat_id,
                    "Выберите класс с помощью кнопки.",
                    grade_keyboard(),
                )
                return
            await crud.set_student_grade(user.id, grade)
            await _send(
                chat_id,
                f"Выбран {grade} класс. Теперь напиши букву класса, например «А».",
            )
            return

        if payload.startswith("link_approve:") or payload.startswith("link_reject:"):
            if user is None or user.role != UserRole.STUDENT or user.status != RequestStatus.APPROVED:
                await acknowledge_callback()
                await _send(
                    chat_id,
                    "Подтверждать запросы может только зарегистрированный ученик.",
                    _restart_keyboard(user),
                )
                return
            approve = payload.startswith("link_approve:")
            try:
                link_id = int(payload.split(":", 1)[1])
            except ValueError:
                await acknowledge_callback()
                await _send(chat_id, "Некорректный запрос.", _restart_keyboard(user))
                return
            decision = await crud.decide_parent_student_link(
                link_id, max_id, approve=approve
            )
            if decision is None:
                await acknowledge_callback()
                await _send(
                    chat_id,
                    "Запрос уже обработан или предназначен другому ученику.",
                    _restart_keyboard(user),
                )
                return
            if approve:
                student_response = (
                    f"Вы подтвердили привязку родителя {decision.parent_name}."
                )
                notice = (
                    f"🎉 Ученик {decision.student_name} подтвердил синхронизацию! "
                    "Теперь вы можете просматривать его учебную сводку."
                )
            else:
                student_response = (
                    f"Вы отклонили запрос родителя {decision.parent_name}."
                )
                notice = f"К сожалению, запрос на синхронизацию с {decision.student_name} был отклонен."
            if can_edit_callback_message:
                try:
                    await event.edit(text=student_response, attachments=[])
                except Exception:
                    logger.exception("Could not edit parent-link callback message")
                    await acknowledge_callback()
                    await _send(chat_id, student_response)
            else:
                await acknowledge_callback()
                await _send(chat_id, student_response)
            try:
                await _send(None, notice, user_id=decision.parent_max_user_id)
            except Exception:
                logger.exception(
                    "Could not notify parent MAX user %s about link decision",
                    decision.parent_max_user_id,
                )
            return

        if user is None:
            await acknowledge_callback()
            await _send(
                chat_id,
                "Для начала работы зарегистрируйтесь.",
                _restart_keyboard(None),
            )
            return
        if payload.startswith("parent:select_child:"):
            if user.role != UserRole.PARENT or user.status != RequestStatus.APPROVED:
                await _send(
                    chat_id,
                    "Эта функция доступна только родителю.",
                    _restart_keyboard(user),
                )
                return
            student_id = int(payload.rsplit(":", 1)[-1])
            student = await mvp.user_by_id(student_id)
            if student is None:
                await _send(
                    chat_id,
                    "Ученик не найден.",
                    keyboard((("👨‍👩‍👧 Синхронизироваться", "parent:sync"),)),
                )
            else:
                await _request_link(chat_id, user, student)
            return

        if payload.startswith("menu:"):
            if payload != f"menu:{_role_value(user.role)}":
                await _send(
                    chat_id,
                    "Этот кабинет доступен только вашей роли.",
                    _restart_keyboard(user),
                )
                return
            # Выход в меню сбрасывает незавершённый шаг ввода (FSM), чтобы
            # пользователь не застревал в диалоге.
            await _set_state(user, None)
            await _menu(chat_id, user, int(max_id))
            return

        # Мини веб-приложение: нативная кнопка MAX (открывается внутри мессенджера).
        if payload == "ui:miniapp":
            if user is None or user.status != RequestStatus.APPROVED:
                await _send(
                    chat_id,
                    "Мини-приложение доступно после регистрации.",
                    _restart_keyboard(user),
                )
                return
            role = _role_value(user.role)
            await _resolve_bot_username()
            await _send(
                chat_id,
                "🕹 Мини-приложение «Школьный штаб» — те же функции в пиксельном "
                "интерфейсе, но под вашу роль: ничего лишнего.\n\n"
                "Нажмите «Открыть мини-приложение» — оно запустится прямо в MAX, "
                "вход автоматический.",
                mini_app_keyboard(role, **_mini_app_args(user)),
            )
            return

        action_roles = {
            "student:": UserRole.STUDENT,
            "teacher:": UserRole.TEACHER,
            "parent:": UserRole.PARENT,
            "psychologist:": UserRole.PSYCHOLOGIST,
        }
        required_role = next(
            (role for prefix, role in action_roles.items() if payload.startswith(prefix)),
            None,
        )
        if (
            required_role is not None
            and (user.role != required_role or user.status != RequestStatus.APPROVED)
        ):
            await _send(
                chat_id,
                "У вас нет доступа к этой функции.",
                _restart_keyboard(user),
            )
            return

        if payload == "teacher:create_class":
            await _set_state(user, "teacher_class_name")
            await _send(chat_id, "Введите название класса, например 10Б.")
            return
        if payload == "teacher:classes":
            classes = await crud.classes_for_teacher(user.id)
            if not classes:
                await _send(chat_id, "Пока нет созданных классов.", menu_keyboard("teacher"))
                return
            try:
                listing = "\n".join(
                    f"• {item.name} — логин: {item.login}; "
                    f"пароль: {crud.decrypt_class_password(item.password_encrypted)}."
                    for item in classes
                )
            except RuntimeError:
                logger.exception("Could not decrypt class passwords for teacher %s", user.id)
                await _send(
                    chat_id,
                    "Не удалось расшифровать пароль. Проверьте CLASS_PASSWORD_KEY в настройках.",
                )
                return
            await _send(chat_id, listing, menu_keyboard("teacher"))
            return
        if payload == "teacher:homework":
            await _ask_owner_class(
                chat_id, user, "teacher:homework", "📝 Домашка. Выберите класс:"
            )
            return
        if payload.startswith("teacher:homework:class:"):
            class_name = payload.split(":", 3)[3]
            await _set_state(user, "hw_subject", class_name=class_name)
            await _send(
                chat_id,
                f"Домашка для {class_name}. Введите предмет, например «Алгебра».",
            )
            return
        if payload == "teacher:hw_mark":
            await _show_teacher_hw_mark(chat_id, user)
            return
        if payload.startswith("teacher:hw_roster:"):
            homework_id = int(payload.rsplit(":", 1)[-1])
            await _show_teacher_hw_roster(chat_id, user, homework_id)
            return
        if payload.startswith("teacher:hw_toggle:"):
            _, _, hw_raw, student_raw = payload.split(":", 3)
            homework_id, student_id = int(hw_raw), int(student_raw)
            roster = await mvp.homework_roster(user, homework_id)
            if roster is None:
                await _send(chat_id, "Задание не найдено или вы не его автор.")
                return
            current = {student.id: done for student, done in roster}
            await mvp.set_homework_completion(
                user, homework_id, student_id, not current.get(student_id, False)
            )
            await _show_teacher_hw_roster(chat_id, user, homework_id)
            return
        if payload.startswith("teacher:hw_points:"):
            # Быстрый выбор баллов кнопкой вместо ввода числа.
            state = _get_state(max_id)
            if not state or state.get("step") != "hw_points" or "due" not in state:
                await _send(
                    chat_id,
                    "Сначала задай предмет, задание и срок — затем выбери баллы.",
                    keyboard((("📝 Домашка", "teacher:homework"),)),
                )
                return
            try:
                points = int(payload.rsplit(":", 1)[-1])
            except ValueError:
                points = -1
            if points not in range(0, 101):
                await _send(
                    chat_id,
                    "Баллы должны быть целым числом от 0 до 100.",
                    keyboard((("📝 Домашка", "teacher:homework"),)),
                )
                return
            await _create_homework(chat_id, user, state, points)
            return
        if payload == "teacher:schedule":
            await _ask_owner_class(
                chat_id, user, "teacher:schedule", "🗓 Расписание. Выберите класс:"
            )
            return
        if payload.startswith("teacher:schedule:class:"):
            class_name = payload.split(":", 3)[3]
            await _show_teacher_class_schedule(chat_id, user, class_name)
            return
        if payload.startswith("teacher:schedule_add:"):
            class_name = payload.split(":", 2)[2]
            await _set_state(user, "sch_day", class_name=class_name)
            await _send(
                chat_id,
                f"Добавление урока для класса {class_name}. Выберите день недели:",
                _weekday_keyboard("teacher:schedule_day"),
            )
            return
        if payload.startswith("teacher:lesson_del:"):
            _, _, lesson_id, class_name = payload.split(":", 3)
            deleted = await mvp.delete_schedule_item(user.id, int(lesson_id))
            await _send(
                chat_id,
                "🗑 Урок удалён."
                if deleted
                else "Не удалось удалить урок: удалять можно только добавленные вами уроки.",
            )
            await _show_teacher_class_schedule(chat_id, user, class_name)
            return
        if payload == "teacher:deadline":
            await _ask_owner_class(
                chat_id, user, "teacher:deadline", "⏰ Контрольные. Выберите класс:"
            )
            return
        if payload.startswith("teacher:deadline:class:"):
            class_name = payload.split(":", 3)[3]
            await _set_state(user, "dl_title", class_name=class_name)
            await _send(
                chat_id,
                f"Контрольная для {class_name}. Введите название, "
                "например «Алгебра — контрольная по теме 3».",
            )
            return
        if payload == "teacher:announce":
            await _ask_owner_class(
                chat_id, user, "teacher:announce", "📢 Объявление. Выберите класс:"
            )
            return
        if payload.startswith("teacher:announce:class:"):
            class_name = payload.split(":", 3)[3]
            await _set_state(user, "announce_text", class_name=class_name)
            await _send(chat_id, f"Введите текст объявления для {class_name}.")
            return
        if payload == "teacher:stats":
            assigned, completed = await mvp.today_counts_for_teacher(user)
            report = await mvp.teacher_completion_report(user)
            lines = [
                "📊 Статистика класса",
                f"Активных заданий: {assigned}",
                f"Отметок о выполнении: {completed}",
            ]
            if report:
                lines.append("Ученики:")
                lines.extend(
                    f"  • {name}: {done}/{total}" for name, done, total in report
                )
            else:
                lines.append("В ваших классах пока нет учеников.")
            await _send(chat_id, "\n".join(lines), menu_keyboard("teacher"))
            return
        if payload == "teacher:settings":
            classes = await crud.classes_for_teacher(user.id)
            names = ", ".join(item.name for item in classes) or "нет"
            await _send(
                chat_id,
                "⚙️ Настройки учителя\n"
                f"Школа: {user.school_name or 'не указана'}\n"
                f"Классы: {names}\n"
                f"Напоминания: {'включены' if user.reminders_enabled else 'выключены'}",
                keyboard(
                    (("🔔 Напоминания", "settings:reminders"),),
                    (("🏫 Школа", "settings:set_school"),),
                    (("⬅️ В меню", "menu:teacher"),),
                ),
            )
            return
        if payload.startswith(("teacher:schedule_day:", "student:schedule_day:")):
            state = _get_state(max_id) or {}
            if user.role != UserRole.TEACHER or state.get("step") != "sch_day":
                await _send(
                    chat_id,
                    "Уроки добавляет только учитель — через меню «🗓 Расписание».",
                    menu_keyboard(_role_value(user.role)),
                )
                return
            day = int(payload.rsplit(":", 1)[-1])
            await _set_state(
                user, "sch_time", class_name=state.get("class_name"), day=day
            )
            await _send(
                chat_id,
                f"{_weekday_label(day)}. Введите время урока в формате ЧЧ:ММ, "
                "например 08:30.",
            )
            return
        if payload.startswith("student:dl_kind:"):
            await _send(
                chat_id,
                "Дедлайны создаёт учитель. Свои контрольные и сроки вы видите "
                "в разделе «⏰ Дедлайны».",
                keyboard(
                    (("⏰ Дедлайны", "student:deadlines"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload.startswith("teacher:dl_kind:"):
            state = _get_state(max_id) or {}
            if state.get("step") != "dl_kind":
                await _send(
                    chat_id,
                    "Сначала добавьте дедлайн через меню.",
                    menu_keyboard(_role_value(user.role)),
                )
                return
            kind = payload.rsplit(":", 1)[-1]
            due = state["due"]
            class_name = state.get("class_name")
            item = await mvp.create_deadline(
                user.id, user.id, state["title"], due, kind, class_name
            )
            await _set_state(user, None)
            target = f" для {class_name}" if class_name else ""
            await _send(
                chat_id,
                f"⏰ Дедлайн{target} добавлен:\n"
                f"{item.title} ({kind}) — {due:%d.%m.%Y %H:%M}",
                menu_keyboard(_role_value(user.role)),
            )
            if user.role == UserRole.TEACHER and class_name:
                await _notify_parents_about_deadline(class_name, user, item, kind, due)
            return
        if payload == "student:join_class":
            await _set_state(user, "student_join_login")
            await _send(chat_id, "Введите логин класса.")
            return
        if payload == "student:schedule":
            await _show_student_schedule(chat_id, user, datetime.now().isoweekday())
            return
        if payload.startswith("student:schedule:"):
            await _show_student_schedule(chat_id, user, int(payload.rsplit(":", 1)[-1]))
            return
        if payload == "student:schedule_week":
            if not user.class_name:
                await _show_student_schedule(chat_id, user, datetime.now().isoweekday())
                return
            lines = [f"📚 Расписание класса {user.class_name} на неделю:"]
            for week_day in range(1, 8):
                day_items = await mvp.schedule_for_day(user, week_day)
                lines.append(f"{_weekday_label(week_day)}:")
                if day_items:
                    for item in day_items:
                        room = f", каб. {item.room}" if item.room else ""
                        lines.append(f"  {item.time:%H:%M} — {item.subject}{room}")
                else:
                    lines.append("  уроков нет")
            await _send(
                chat_id,
                "\n".join(lines),
                keyboard(
                    (("⬅️ Расписание по дням", "student:schedule"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload.startswith("student:lesson_del:"):
            await _send(
                chat_id,
                "Ученики не могут удалять уроки: расписание класса ведёт учитель. "
                "Если в расписании ошибка — сообщите классному руководителю.",
                keyboard(
                    (("📚 Расписание", "student:schedule"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:schedule_add":
            await _send(
                chat_id,
                "Ученики не могут добавлять уроки: расписание класса загружает "
                "учитель — оно появится у вас сразу, как только он его заполнит.",
                keyboard(
                    (("📚 Расписание", "student:schedule"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:homework":
            await _show_student_homework(chat_id, user)
            return
        if payload == "student:homework_add":
            await _send(
                chat_id,
                "Домашние задания задаёт учитель по своему предмету — они "
                "появятся в списке автоматически. Здесь можно отмечать сдачу ✅.",
                keyboard(
                    (("📝 Домашка", "student:homework"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload.startswith("student:homework_done:"):
            await _send(
                chat_id,
                "Сдачу домашних заданий отмечает учитель 👩🏫 — "
                "статус обновится, когда она проверит работу.",
            )
            await _show_student_homework(chat_id, user)
            return
        if payload == "student:deadlines":
            items = await mvp.upcoming_deadlines(user)
            await _send(
                chat_id,
                _render_deadlines(items),
                keyboard((("⬅️ В меню", "menu:student"),)),
            )
            return
        if payload == "student:deadline_add":
            await _send(
                chat_id,
                "Дедлайны (контрольные, проекты, зачёты) задаёт учитель — они "
                "появляются у класса автоматически. Напоминания приходят за "
                "неделю, за 3 дня и за день до срока; сроки можно изменить в "
                "«⚙️ Настройки» → «🔔 Напоминания».",
                keyboard(
                    (("⏰ Дедлайны", "student:deadlines"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:announcements":
            items = await mvp.student_announcements(user)
            await _send(
                chat_id,
                _render_announcements(items),
                keyboard((("⬅️ В меню", "menu:student"),)),
            )
            return
        if payload == "student:stress":
            await _send(
                chat_id,
                "🧘 Антистресс\nВыбери, что нужно прямо сейчас:",
                keyboard(
                    (("🙂 Как ты сегодня?", "student:mood_ask"),),
                    (("🌬 Дыхательная практика", "student:breathing"),),
                    (("🃏 Антистресс-карточка", "student:stress_card"),),
                    (("📖 Дневник настроения", "student:mood_history"),),
                    (("🤖 Чат с ИИ-психологом", "student:ai_chat"),),
                    (("💬 Поговорить с психологом", "student:talk_psychologist"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:mood_ask":
            await _send(
                chat_id,
                "Оцени своё состояние от 1 (тяжело) до 5 (отлично):",
                keyboard(
                    (("1", "student:mood:1"), ("2", "student:mood:2"), ("3", "student:mood:3")),
                    (("4", "student:mood:4"), ("5", "student:mood:5")),
                ),
            )
            return
        if payload == "student:mood_history":
            entries = await mvp.mood_history(user.id, 14)
            await _send(
                chat_id,
                _render_mood(entries),
                keyboard(
                    (("🙂 Отметить сегодня", "student:mood_ask"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload.startswith("student:mood:"):
            mood_score = int(payload.rsplit(":", 1)[-1])
            gain = await _mood_gain_safe(user.id)
            saved = await mvp.save_mood(user, mood_score)
            if saved and mood_score <= 3:
                # Оценка 3 и ниже — предлагаем поддержку психолога.
                await _send(
                    chat_id,
                    f"Спасибо, что поделился! +{gain} {_points_word(gain)} 🌱\n"
                    "Вижу, что день выдался непростым. Ты не один: можно "
                    "выговориться ИИ-психологу или передать сигнал школьному "
                    "психологу — анонимно и только с твоего согласия.",
                    keyboard(
                        (("🤖 Чат с ИИ-психологом", "student:ai_chat"),),
                        (("💬 Связаться с психологом", "stress:yes"),),
                        (("⬅️ В меню", "menu:student"),),
                    ),
                )
                return
            await _send(
                chat_id,
                f"Спасибо, что поделился! +{gain} {_points_word(gain)} 🌱"
                if saved
                else "Настроение за сегодня уже отмечено. Возвращайся завтра 🙂",
                keyboard(
                    (("📊 Дневник настроения", "student:mood_history"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:breathing":
            await _send(
                chat_id,
                BREATHING_PRACTICE,
                keyboard((("⬅️ В меню", "menu:student"),)),
            )
            return
        if payload == "student:stress_card":
            owned = await mvp.user_reward_keys(user.id)
            pool = STRESS_CARDS + (RARE_STRESS_CARDS if "card_pack" in owned else ())
            title, body = random.choice(pool)
            await _send(
                chat_id,
                f"{title}\n{body}",
                keyboard(
                    (("🔄 Другую карточку", "student:stress_card"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:ai_chat":
            ai_psychologist.reset_session(max_id)
            await _set_state(user, "ai_chat")
            await _send(
                chat_id,
                "🤖 Я — ИИ-психолог «Школьного штаба».\n"
                "Напиши, что у тебя на душе, — я выслушаю, поддержу и помогу "
                "разобраться в чувствах. Отвечаю я, а не живой человек: "
                "я не ставлю диагнозы и не заменяю специалиста.\n"
                "Диалог конфиденциален: его не видят ни учителя, ни родители.\n\n"
                "Просто напиши сообщение — или заверши диалог кнопкой ниже.",
                keyboard((("⬅️ Завершить диалог", "student:ai_chat_exit"),)),
            )
            return
        if payload == "student:ai_chat_exit":
            await _set_state(user, None)
            ai_psychologist.reset_session(max_id)
            await _send(
                chat_id,
                "Диалог завершён. Я всегда на связи 🤍 Береги себя.",
                keyboard(
                    (("🧘 Антистресс", "student:stress"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:talk_psychologist":
            await _set_state(user, "stress_format")
            await _send(
                chat_id,
                "Как тебе удобнее связаться с психологом?",
                keyboard(
                    (("Вживую", "stress:live"), ("Переписка", "stress:chat")),
                    (("⏳ Позже", "stress:later"),),
                ),
            )
            return
        if payload == "student:settings":
            survey_hour = (
                getattr(user, "mood_survey_hour", None)
                or scheduler.DEFAULT_SURVEY_HOUR
            )
            await _send(
                chat_id,
                "⚙️ Настройки\n"
                f"Класс: {user.class_name or 'не указан'}\n"
                f"Школа: {user.school_name or 'не указана'}\n"
                f"Очки: {user.points}\n"
                f"Напоминания: {'включены' if user.reminders_enabled else 'выключены'}\n"
                f"Время опроса «Как ты сегодня?»: {survey_hour}:00",
                keyboard(
                    (("🔔 Напоминания", "settings:reminders"),),
                    (("⏰ Время опроса", "student:survey_time"),),
                    (("🏫 Школа", "settings:set_school"), ("🎓 Класс", "settings:set_class")),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:survey_time":
            await _set_state(user, "survey_time")
            await _send(
                chat_id,
                "Во сколько присылать ежедневный опрос «Как ты сегодня?»\n"
                f"Введи час от {scheduler.SURVEY_HOUR_MIN} до "
                f"{scheduler.SURVEY_HOUR_MAX}, например 18.",
                keyboard(
                    (
                        ("16:00", "student:survey_hour:16"),
                        ("18:00", "student:survey_hour:18"),
                        ("20:00", "student:survey_hour:20"),
                    ),
                    (("⚙️ Настройки", "student:settings"),),
                ),
            )
            return
        if payload.startswith("student:survey_hour:"):
            try:
                hour = int(payload.rsplit(":", 1)[-1])
            except ValueError:
                hour = -1
            if hour not in range(
                scheduler.SURVEY_HOUR_MIN, scheduler.SURVEY_HOUR_MAX + 1
            ):
                await _send(
                    chat_id,
                    f"Доступно время с {scheduler.SURVEY_HOUR_MIN}:00 до "
                    f"{scheduler.SURVEY_HOUR_MAX}:00.",
                )
                return
            await _save_survey_hour(chat_id, max_id, user, hour)
            return
        if payload == "parent:sync":
            await _set_state(user, "onboarding_parent_child")
            await _send(
                chat_id,
                "Введи числовой MAX ID ребёнка. Также можно указать точное ФИО "
                "(MAX ID можно посмотреть в профиле ученика).",
            )
            return
        if payload == "student:profile":
            owned = await mvp.user_reward_keys(user.id)
            badges = "".join(
                REWARD_PROFILE_EMOJI[key]
                for key in owned
                if key in REWARD_PROFILE_EMOJI
            )
            display_name = f"{user.name} {badges}" if badges else user.name
            lines = [
                f"Профиль: {display_name}",
                f"Класс: {user.class_name or 'не указан'}",
                f"Школа: {user.school_name or 'не указана'}",
                f"Уникальный MAX ID для привязки родителя: {user.max_user_id}",
                f"Очки: {user.points}",
            ]
            owned_titles = [
                mvp.REWARD_CATALOG[key][0]
                for key in mvp.REWARD_CATALOG
                if key in owned
            ]
            if owned_titles:
                lines.append(
                    "Титулы и оформление: " + ", ".join(owned_titles)
                )
            else:
                lines.append(
                    "Титулы и оформление: пока ничего не открыто — "
                    "загляни в «🎁 Награды и кастомизация»."
                )
            await _send(
                chat_id,
                "\n".join(lines),
                keyboard(
                    (("🎁 Награды и кастомизация", "student:rewards"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload == "student:rewards":
            await _show_student_rewards(chat_id, user)
            return
        if payload.startswith("student:rewards:"):
            try:
                page = int(payload.rsplit(":", 1)[-1])
            except ValueError:
                page = 0
            await _show_student_rewards(chat_id, user, page)
            return
        if payload == "student:spin":
            owned = await mvp.user_reward_keys(user.id)
            if "daily_spin" not in owned:
                await _send(
                    chat_id,
                    "🎰 Колесо удачи открывается одноимённой наградой "
                    f"за {mvp.REWARD_CATALOG['daily_spin'][1]} очков.",
                    keyboard(
                        (("🎁 Награды", "student:rewards"),),
                        (("⬅️ В меню", "menu:student"),),
                    ),
                )
                return
            prize = await mvp.daily_spin(user)
            if prize is None:
                await _send(
                    chat_id,
                    "🎰 Сегодня ты уже крутил колесо — возвращайся завтра 🌙",
                    keyboard(
                        (("🎁 Награды", "student:rewards"),),
                        (("🎮 Профиль и очки", "student:profile"),),
                        (("⬅️ В меню", "menu:student"),),
                    ),
                )
                return
            user = await mvp.user_by_max_id(max_id) or user
            result = (
                f"🎰 Барабан крутится… 🎉 Тебе выпало {prize} очков!"
                if prize
                else "🎰 Барабан крутится… В этот раз пусто — завтра новый шанс!"
            )
            await _send(
                chat_id,
                result + f"\nТвои очки: {user.points} 🪙",
                keyboard(
                    (("🎁 Награды", "student:rewards"),),
                    (("🎮 Профиль и очки", "student:profile"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
            )
            return
        if payload.startswith("student:reward_buy:"):
            item_key = payload.rsplit(":", 1)[-1]
            entry = mvp.REWARD_CATALOG.get(item_key)
            if entry is None:
                await _send(
                    chat_id,
                    "Такой награды нет в списке.",
                    keyboard((("🎁 Награды", "student:rewards"),)),
                )
                return
            title, cost, _description = entry
            page = list(mvp.REWARD_CATALOG).index(item_key) // REWARDS_PER_PAGE
            if await mvp.unlock_reward(user, item_key, cost):
                user = await mvp.user_by_max_id(max_id) or user
                await _send(
                    chat_id,
                    f"🎉 Награда открыта: {title}! Списано {cost} очков.\n"
                    f"Осталось очков: {user.points}",
                )
            else:
                await _send(
                    chat_id,
                    "Не получилось: награда уже открыта или не хватает очков "
                    f"(стоимость {cost} 🪙).",
                )
            await _show_student_rewards(chat_id, user, page)
            return
        if payload == "parent:summary":
            links = await crud.approved_parent_links(user.id)
            if not links:
                await _send(chat_id, "Пока нет подтвержденных связей с учениками.")
                return
            summaries = []
            for link in links:
                student, completed, overdue, deadlines = await mvp.parent_summary(
                    user, student_id=link.student.id
                )
                if student is None:
                    continue
                summary = (
                    f"• {student.name} ({student.class_name or 'класс не указан'}): "
                    f"выполнено за неделю — {completed}, просрочено — {overdue}."
                )
                if deadlines:
                    summary += "\n  Ближайшие сроки: " + "; ".join(
                        f"{item.title} — {item.date:%d.%m.%Y}" for item in deadlines
                    )
                summaries.append(summary)
            await _send(
                chat_id,
                "Учебная сводка:\n" + "\n".join(summaries)
                if summaries
                else "Для учеников пока нет данных учебной сводки.",
            )
            return
        if payload == "parent:notifications":
            await _send(
                chat_id,
                (
                    "🔔 Уведомления включены."
                    if user.reminders_enabled
                    else "🔕 Уведомления выключены."
                )
                + "\nВы получаете сообщения о новых контрольных и ближайших "
                "учебных событиях ребёнка.",
                keyboard(
                    (("Изменить настройки", "settings:reminders"),),
                    (("📅 Ближайшие события", "parent:deadlines"),),
                ),
            )
            return
        if payload == "parent:deadlines":
            items = await mvp.deadlines_for_parent(user)
            await _send(
                chat_id,
                _render_deadlines(items[:10]),
                keyboard((("⬅️ В меню", "menu:parent"),)),
            )
            return
        if payload == "settings:set_school":
            await _set_state(user, "set_school")
            await _send(
                chat_id,
                "Введи название школы, например «Школа №1». "
                "Оно нужно, чтобы находить психолога и одноклассников.",
            )
            return
        if payload == "settings:set_class":
            await _set_state(user, "set_class")
            await _send(
                chat_id,
                "Введи свой класс одной строкой, например «9А».",
            )
            return
        if payload == "settings:reminders":
            if (
                user.role not in {UserRole.STUDENT, UserRole.PARENT, UserRole.TEACHER}
                or user.status != RequestStatus.APPROVED
            ):
                await _send(
                    chat_id,
                    "У вас нет доступа к этой функции.",
                    _restart_keyboard(user),
                )
                return
            await _show_reminder_settings(chat_id, user)
            return
        if payload.startswith("settings:reminders:"):
            if (
                user.role not in {UserRole.STUDENT, UserRole.PARENT, UserRole.TEACHER}
                or user.status != RequestStatus.APPROVED
            ):
                await _send(
                    chat_id,
                    "У вас нет доступа к этой функции.",
                    _restart_keyboard(user),
                )
                return
            level = payload.rsplit(":", 1)[-1]
            field = {
                "all": "reminders_enabled",
                "7d": "notify_7d",
                "3d": "notify_3d",
                "1d": "notify_1d",
            }.get(level)
            if field is None:
                await _send(chat_id, "Неизвестная настройка напоминаний.")
                return
            current = bool(getattr(user, field, True))
            await mvp.update_profile(max_id, **{field: not current})
            refreshed = await mvp.user_by_max_id(max_id)
            await _show_reminder_settings(chat_id, refreshed or user)
            return
        if payload == "psychologist:signals":
            signals = await mvp.psychologist_signals(user.id)
            if not signals:
                await _send(
                    chat_id,
                    "Пока нет сигналов от учеников.",
                    menu_keyboard("psychologist"),
                )
                return
            text_lines = []
            signal_rows: list[tuple[tuple[str, str], ...]] = []
            for signal in signals:
                student = signal.student
                text_lines.append(
                    f"🚨 Сигнал #{signal.id}\n"
                    f"Ученик: {student.name or 'Неизвестно'} "
                    f"({student.class_name or 'класс не указан'})\n"
                    f"Риск: {signal.risk_level}/5\n"
                    f"Темы: {signal.themes}\n"
                    f"Статус: {signal.status}\n"
                    f"Формат: {signal.preferred_format}"
                )
                signal_rows.append(
                    (
                        (
                            f"🟢 #{signal.id} В работу",
                            f"psychologist:signal:{signal.id}:in_progress",
                        ),
                        (
                            f"✅ #{signal.id} Закрыть",
                            f"psychologist:signal:{signal.id}:closed",
                        ),
                    )
                )
                signal_rows.append(
                    (
                        (
                            f"✉️ #{signal.id} Связаться",
                            f"psychologist:contact:{signal.id}",
                        ),
                    )
                )
            signal_rows.append((("⬅️ В меню", "menu:psychologist"),))
            await _send(
                chat_id,
                "\n\n".join(text_lines),
                keyboard(*signal_rows),
            )
            return
        if payload == "psychologist:stats":
            summary = await mvp.psychologist_signals_summary(user.id)
            await _send(
                chat_id,
                f" Статистика сигналов\n"
                f"Всего: {summary['total']}\n"
                f"Ожидают: {summary['pending']}\n"
                f"В работе: {summary['in_progress']}\n"
                f"Закрыты: {summary['closed']}",
                menu_keyboard("psychologist"),
            )
            return
        if payload == "psychologist:settings":
            await _send(
                chat_id,
                "Настройки психолога.\n"
                f"Школа: {user.school_name or 'не указана'}\n"
                f"ИИ-ассистент: {config.AI_PSYCHOLOGIST_MODEL} — "
                f"{'подключён' if ai_psychologist.is_configured() else 'не настроен (нужен ключ в .env)'}\n"
                f"Уведомления: {'включены' if user.reminders_enabled else 'выключены'}.",
                keyboard(
                    (("🏫 Школа", "settings:set_school"),),
                    (("⬅️ В меню", "menu:psychologist"),),
                ),
            )
            return
        if payload.startswith("psychologist:signal:"):
            parts = payload.split(":")
            if len(parts) == 4 and parts[3] in {"pending", "in_progress", "closed"}:
                signal_id = int(parts[2])
                new_status = parts[3]
                updated = await mvp.update_signal_status(signal_id, new_status)
                if updated:
                    await _send(
                        chat_id,
                        f"Статус сигнала #{signal_id} изменён на {new_status}.",
                        menu_keyboard("psychologist"),
                    )
                else:
                    await _send(chat_id, "Сигнал не найден.")
            return
        if payload.startswith("psychologist:contact:"):
            signal_id = int(payload.rsplit(":", 1)[-1])
            signal = await mvp.psychologist_signal(user.id, signal_id)
            if signal is None:
                await _send(chat_id, "Сигнал не найден.", menu_keyboard("psychologist"))
                return
            student = signal.student
            preferred = (
                "живая встреча" if signal.preferred_format == "live" else "переписка"
            )
            await _send(
                None,
                "Привет! Это школьный психолог 🧠\n"
                f"Ты выбрал(а) формат «{preferred}» — я на связи.\n"
                "Если хочешь поговорить, просто ответь на это сообщение или "
                "подойди в кабинет психолога в удобное время.\n"
                "Всё, чем ты поделишься, останется между нами.",
                keyboard(
                    (("🧘 Антистресс", "student:stress"),),
                    (("⬅️ В меню", "menu:student"),),
                ),
                user_id=int(student.max_user_id),
            )
            await mvp.update_signal_status(signal_id, "in_progress")
            await _send(
                chat_id,
                f"✉️ Сообщение ученику отправлено (формат: {preferred}). "
                f"Сигнал #{signal_id} отмечен как «в работе».",
                menu_keyboard("psychologist"),
            )
            return
        if payload == "parent:tips":
            await _send(
                chat_id,
                "💡 Короткие советы по поддержке подростка:\n"
                "• Спрашивайте «как прошёл день?», а не «что получил?» — "
                "так ребёнок охотнее делится.\n"
                "• Хвалите за усилия и регулярность, а не только за результат.\n"
                "• Дедлайн ближе, чем кажется: предложите помощь с планом, "
                "но не делайте задание вместо ребёнка.\n"
                "• Если ребёнок замкнулся или плохо спит — это повод "
                "обратиться к школьному психологу.\n"
                "• Договоритесь о времени без телефона перед сном: "
                "сон сильнее влияет на оценки, чем ещё час занятий.",
                keyboard((("⬅️ В меню", "menu:parent"),)),
            )
            return
        if payload == "parent:settings":
            children = await mvp.parent_children(user)
            names = ", ".join(
                f"{item.name or 'Без имени'} ({item.class_name or 'класс не указан'})"
                for item in children
            ) or "пока нет подтвержденных связей"
            await _send(
                chat_id,
                "⚙️ Настройки родителя\n"
                f"Дети: {names}\n"
                f"Уведомления: {'включены' if user.reminders_enabled else 'выключены'}",
                keyboard(
                    (("🔔 Напоминания", "settings:reminders"),),
                    (("👨‍‍👧 Привязать ребёнка", "parent:sync"),),
                    (("🏫 Школа", "settings:set_school"),),
                    (("⬅️ В меню", "menu:parent"),),
                ),
            )
            return
        if payload.startswith("parent:") and payload not in {
            "parent:notifications",
            "parent:deadlines",
            "parent:tips",
            "parent:settings",
        }:
            await _send(
                chat_id,
                "Неизвестное действие родительского кабинета.",
                _restart_keyboard(user),
            )
            return
        if payload.startswith("psychologist:") and payload not in {
            "psychologist:signals",
            "psychologist:stats",
            "psychologist:settings",
        }:
            await _send(
                chat_id,
                "Неизвестное действие кабинета психолога.",
                _restart_keyboard(user),
            )
            return
        if payload.startswith("stress:"):
            if user.role != UserRole.STUDENT or user.status != RequestStatus.APPROVED:
                await _send(
                    chat_id,
                    "Эта функция доступна только ученикам.",
                    _restart_keyboard(user),
                )
                return
            action = payload.split(":", 1)[-1]
            if action == "yes":
                await _set_state(user, "stress_format")
                await _send(
                    chat_id,
                    "Как тебе удобнее связаться с психологом?",
                    keyboard(
                        (("Вживую", "stress:live"), ("Переписка", "stress:chat"),),
                        (("⏳ Позже", "stress:later"),),
                    ),
                )
                return
            if action in {"live", "chat"}:
                await _start_stress_signal(chat_id, user, action)
                return
            if action == "help":
                await _set_state(user, None)
                await _send(
                    chat_id,
                    CRISIS_CONTACTS,
                    keyboard(
                        (("💬 Поговорить с психологом", "student:talk_psychologist"),),
                        (("🤖 Чат с ИИ-психологом", "student:ai_chat"),),
                        (("🧘 Антистресс", "student:stress"),),
                        (("⬅️ В меню", "menu:student"),),
                    ),
                )
                return
            if action == "school_manual":
                state = _get_state(max_id) or {}
                await _set_state(
                    user,
                    "stress_school",
                    stress_format=state.get("stress_format") or "chat",
                    schools=state.get("schools") or [],
                )
                await _send(
                    chat_id,
                    "Напиши название школы одним сообщением, например «Школа №1».\n"
                    "Если школы нет в списке — значит, психолог этой школы ещё не "
                    "зарегистрировался в боте. Сигнал всё равно можно передать "
                    "дежурному психологу.",
                    keyboard(
                        (("⬅️ К списку школ", "stress:school_list"),),
                        (("⬅️ В меню", "menu:student"),),
                    ),
                )
                return
            if action == "school_list":
                state = _get_state(max_id) or {}
                await _ask_psychologist_school(
                    chat_id,
                    user,
                    state.get("stress_format") or "chat",
                    "Выбери школу — я найду её психолога.",
                )
                return
            if action.startswith("school:"):
                state = _get_state(max_id) or {}
                schools = state.get("schools") or []
                try:
                    index = int(action.split(":", 1)[-1])
                    school = schools[index]
                except (ValueError, IndexError):
                    await _send(
                        chat_id,
                        "Список школ устарел — открой его заново.",
                        keyboard(
                            (("🏫 Выбрать школу", "stress:school_list"),),
                            (("⬅️ В меню", "menu:student"),),
                        ),
                    )
                    return
                await mvp.update_profile(str(user.max_user_id), school=school)
                user.school_name = school
                await _set_state(user, None)
                await _send(chat_id, f"🏫 Школа сохранена: {school}")
                await _start_stress_signal(
                    chat_id, user, state.get("stress_format") or "chat"
                )
                return
            if action == "any":
                fallback = await mvp.any_psychologist()
                if fallback is None:
                    await _send(
                        chat_id,
                        "Сейчас в системе нет подтверждённых психологов.\n\n"
                        + CRISIS_CONTACTS,
                        keyboard(
                            (("🤖 Чат с ИИ-психологом", "student:ai_chat"),),
                            (("🧘 Антистресс", "student:stress"),),
                            (("⬅️ В меню", "menu:student"),),
                        ),
                    )
                    return
                state = _get_state(max_id) or {}
                await _set_state(
                    user,
                    "stress_confirm_any",
                    stress_format=state.get("stress_format") or "chat",
                    psychologist_id=fallback.id,
                )
                await _send(
                    chat_id,
                    "Передать сигнал дежурному психологу (он из другой школы)?\n"
                    "Он увидит только имя, класс, школу, уровень стресса и темы — "
                    "нашу переписку он не видит.\n"
                    "Отказаться можно в любой момент.",
                    keyboard(
                        (("✅ Да, передать", "stress:any_confirm"),),
                        (("⬅️ Выбрать школу", "stress:school_list"),),
                        (("🤖 Лучше ИИ-психолог", "student:ai_chat"),),
                    ),
                )
                return
            if action == "any_confirm":
                state = _get_state(max_id) or {}
                psychologist_id = state.get("psychologist_id")
                psychologist = (
                    await mvp.user_by_id(int(psychologist_id))
                    if psychologist_id
                    else None
                ) or await mvp.any_psychologist()
                if psychologist is None:
                    await _send(
                        chat_id,
                        "К сожалению, свободных психологов сейчас нет.\n\n"
                        + CRISIS_CONTACTS,
                        keyboard(
                            (("🤖 Чат с ИИ-психологом", "student:ai_chat"),),
                            (("⬅️ В меню", "menu:student"),),
                        ),
                    )
                    return
                await _deliver_signal(
                    chat_id,
                    user,
                    psychologist,
                    state.get("stress_format") or "chat",
                )
                return
            if action in {"later", "no"}:
                await _set_state(user, None)
                await _send(
                    chat_id,
                    "Хорошо. Если передумаешь, можешь обратиться через меню или просто написать мне.",
                    menu_keyboard("student"),
                )
                return
            await _send(chat_id, "Неизвестное действие.", _restart_keyboard(user))
            return
        await _send(
            chat_id,
            "Эта функция пока недоступна.",
            _restart_keyboard(user),
        )
    except Exception:
        logger.exception("Failed to handle callback %r from user %s", payload, max_id)
        await acknowledge_callback()
        await _send(
            chat_id,
            "Не удалось выполнить действие. Попробуйте еще раз.",
            keyboard((("↩️ Начать заново", "onboard:restart"),)),
        )


@dp.message_created()
async def handle_message_created(event: Any) -> None:
    await _handle_message_created(event)


@dp.message_callback()
async def handle_message_callback(event: Any) -> None:
    await _handle_callback(event)


async def _send_reminder(max_user_id: int, text: str) -> None:
    """Доставить напоминание планировщика.

    Напоминание приходит само, поэтому кнопка возврата в меню обязательна:
    иначе пользователь не может выйти из сообщения без /start.
    """
    role = "student"
    try:
        reminder_user = await mvp.user_by_max_id(str(max_user_id))
        if reminder_user is not None:
            role = _role_value(reminder_user.role)
    except Exception:  # noqa: BLE001 - напоминание важнее точной роли
        logger.exception("Could not resolve role for reminder to %s", max_user_id)
    await _send(
        None,
        text,
        keyboard((("⬅️ В меню", f"menu:{role}"),)),
        user_id=max_user_id,
    )


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN must be set in the environment.")
    await create_db_and_tables()
    # Администратора в продукте нет: активируем аккаунты, которые остались
    # «ожидающими» от прошлых версий бота.
    activated = await mvp.approve_all_pending()
    if activated:
        logger.info("Активировано аккаунтов со старым статусом: %s", activated)
    # Username бота нужен кнопке open_app, чтобы мини-приложение открывалось
    # внутри MAX, а не во внешнем браузере.
    await _resolve_bot_username()

    reminder_task = asyncio.create_task(scheduler.run_forever(_send_reminder))
    logger.info("MAX-бот «Школьный штаб» запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
