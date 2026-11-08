"""JSON API мини веб-приложения: те же функции, что и в меню бота.

Все обработчики переиспользуют сервисы app.services.mvp / app.db.crud,
поэтому данные идентичны тому, что показывает бот в сообщениях.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from maxapi import Bot
from pydantic import BaseModel
from sqlalchemy import func, select

from app.config import BOT_TOKEN
from app.db import crud
from app.db.database import async_session
from app.db.models import Announcement, ClassMember, SchoolClass, User
from app.mini_web.auth import make_token, read_token
from app.services import ai_psychologist, mvp

logger = logging.getLogger(__name__)
router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"
router.mount("/mini/static", StaticFiles(directory=STATIC_DIR), name="mini-static")

# Ассеты страницы /mini: версия считается по времени их изменения.
_ASSET_FILES = ("mini.css", "mini.js")
_asset_version_cache: str | None = None


def _asset_version() -> str:
    """Отпечаток статики для cache-busting (пересчитывается после перезапуска)."""
    global _asset_version_cache  # noqa: PLW0603 - одноразовый кэш на процесс
    if _asset_version_cache is None:
        stamps = []
        for name in _ASSET_FILES:
            try:
                stamps.append(str(int((STATIC_DIR / name).stat().st_mtime)))
            except OSError:  # файла нет — версия не важна
                continue
        _asset_version_cache = "-".join(stamps) or "0"
    return _asset_version_cache

AI_CRISIS_REPLY = (
    "То, о чём ты пишешь, звучит очень серьёзно. Ты не один(на) — рядом есть люди, "
    "готовые помочь.\n"
    "Если чувствуешь, что тебе угрожает опасность, — позвони 103 (скорая помощь) "
    "или на бесплатный анонимный телефон доверия 8-800-2000-122.\n"
    "Я могу передать школьному психологу, что тебе нужна поддержка — "
    "он не увидит нашу переписку, только сам факт обращения."
)


# --------------------------------------------------------------------------- #
# Доступ
# --------------------------------------------------------------------------- #
async def _current_user(request: Request) -> User:
    token = request.headers.get("x-mini-token") or request.query_params.get("token")
    max_id = read_token(token)
    if max_id is None:
        raise HTTPException(status_code=401, detail="Недействительная ссылка. Откройте её из бота.")
    user = await mvp.user_by_max_id(str(max_id))
    if user is None:
        raise HTTPException(status_code=401, detail="Пользователь не найден. Напишите /start боту.")
    return user


def _require_role(user: User, *roles: str) -> None:
    role = getattr(user.role, "value", str(user.role))
    if role not in roles:
        raise HTTPException(status_code=403, detail="Недоступно для вашей роли.")


def _role(user: User) -> str:
    return getattr(user.role, "value", str(user.role))


def _parse_dt(value: str) -> datetime:
    cleaned = (value or "").strip()
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            pass
    raise HTTPException(status_code=422, detail="Неверный формат даты (ожидается ISO или ДД.ММ.ГГГГ).")


def _fmt_dt(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M") if value else None


def _parse_lesson_time(value: str | None) -> time:
    """Время урока: «08:30», «8:30» или «0830»."""
    raw = (value or "").strip()
    if ":" in raw:
        hour_part, _, minute_part = raw.partition(":")
    elif len(raw) in (3, 4) and raw.isdigit():
        hour_part, minute_part = raw[:-2], raw[-2:]
    else:
        raise HTTPException(status_code=422, detail="Время урока — например 08:30.")
    try:
        hour, minute = int(hour_part), int(minute_part[:2])
    except ValueError:
        raise HTTPException(status_code=422, detail="Время урока — например 08:30.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=422, detail="Время урока — например 08:30.")
    return time(hour=hour, minute=minute)


def _day_number(value: int | str | None) -> int:
    """День недели 1–7 (понедельник = 1)."""
    names = {"пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7}
    try:
        day = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        day = names.get(str(value or "").strip().lower()[:2], 0)
    if not 1 <= day <= 7:
        raise HTTPException(status_code=422, detail="День недели — от 1 (пн) до 7 (вс).")
    return day


# --------------------------------------------------------------------------- #
# Состав мини-приложения для каждой роли
# --------------------------------------------------------------------------- #
# Фронтенд строит вкладки и меню по этому списку, поэтому у психолога и
# родителя нет ученических разделов (домашка, антистресс, награды), а у учителя
# появляются инструменты класса.
ROLE_FEATURES: dict[str, tuple[str, ...]] = {
    "student": (
        "home",
        "homework",
        "deadlines",
        "stress",
        "psychologist_signal",
        "schedule",
        "announcements",
        "profile",
        "join_class",
        "settings",
    ),
    "teacher": (
        "home",
        "homework",
        "homework_create",
        "roster_mark",
        "deadlines",
        "deadline_create",
        "schedule",
        "schedule_edit",
        "announcements",
        "announcement_create",
        "classes",
        "stats",
        "settings",
    ),
    "parent": (
        "home",
        "children",
        "summary",
        "deadlines",
        "tips",
        "settings",
    ),
    "psychologist": (
        "home",
        "signals",
        "signal_status",
        "psych_stats",
        "settings",
    ),
}

SIGNAL_STATUSES: tuple[str, ...] = ("pending", "in_progress", "closed")

SIGNAL_STATUS_LABELS = {
    "pending": "ожидает",
    "in_progress": "в работе",
    "closed": "закрыт",
}


def _features(user: User) -> list[str]:
    return list(ROLE_FEATURES.get(_role(user), ("home", "settings")))


def _require_feature(user: User, feature: str) -> None:
    if feature not in _features(user):
        raise HTTPException(status_code=403, detail="Недоступно для вашей роли.")


def _lesson_payload(item: Any, user: User) -> dict:
    return {
        "id": item.id,
        "time": f"{item.time:%H:%M}",
        "subject": item.subject,
        "room": item.room,
        "day": item.day_of_week,
        "class_name": item.class_name,
        # Менять урок может только учитель, который его создал.
        "can_edit": item.created_by == user.id and _role(user) == "teacher",
    }


def _signal_payload(signal: Any) -> dict:
    student = getattr(signal, "student", None)
    return {
        "id": signal.id,
        "student_name": getattr(student, "full_name", None) or "Неизвестно",
        "class_name": getattr(student, "class_name", None),
        "risk_level": signal.risk_level,
        "themes": signal.themes,
        "status": signal.status,
        "status_label": SIGNAL_STATUS_LABELS.get(signal.status, signal.status),
        "preferred_format": signal.preferred_format,
        "created_at": _fmt_dt(signal.created_at),
    }


async def _parent_children_payload(user: User) -> list[dict]:
    """Дети родителя: учебная сводка и ближайшие сроки (как в меню бота)."""
    items: list[dict] = []
    for child in await mvp.parent_children(user):
        student, completed, overdue, deadlines = await mvp.parent_summary(user, child.id)
        if student is None:
            continue
        items.append(
            {
                "id": student.id,
                "name": student.full_name,
                "class_name": student.class_name,
                "grade": student.grade,
                "school": student.school_name,
                "completed_week": completed,
                "overdue": overdue,
                "deadlines": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "kind": item.kind,
                        "class_name": item.class_name,
                        "date": _fmt_dt(item.date),
                    }
                    for item in deadlines
                ],
            }
        )
    return items



# --------------------------------------------------------------------------- #
# Страница приложения
# --------------------------------------------------------------------------- #
@router.get("/mini", include_in_schema=False)
async def mini_page() -> HTMLResponse:
    """Страница приложения.

    Ссылки на статику дополняются версией (mtime файлов), а сама страница
    отдаётся с ``no-store``: WebView в MAX надолго кэширует ассеты, и без этого
    после деплоя пользователь видел бы предыдущую сборку mini.js.
    """
    html = (STATIC_DIR / "mini.html").read_text(encoding="utf-8")
    version = _asset_version()
    for name in _ASSET_FILES:
        html = html.replace(
            f'/mini/static/{name}"', f'/mini/static/{name}?v={version}"'
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/mini/login", include_in_schema=False)
async def mini_login(max_user_id: int = Query(...)) -> dict:
    """Служебный вход по MAX ID (для локальной отладки без бота)."""
    user = await mvp.user_by_max_id(str(max_user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"token": make_token(user.max_user_id)}


# --------------------------------------------------------------------------- #
# Профиль и главная
# --------------------------------------------------------------------------- #
@router.get("/mini/api/me")
async def api_me(request: Request) -> dict:
    user = await _current_user(request)
    owned = await mvp.user_reward_keys(user.id)
    return {
        "name": user.full_name,
        "role": _role(user),
        # Состав интерфейса под роль: фронтенд строит по нему вкладки и меню.
        "features": _features(user),
        "class_name": user.class_name,
        "school": user.school_name,
        "grade": user.grade,
        "points": user.points,
        "reminders_enabled": user.reminders_enabled,
        "notify_7d": user.notify_7d,
        "notify_3d": user.notify_3d,
        "notify_1d": user.notify_1d,
        "ai_configured": ai_psychologist.is_configured(),
        # Доступные темы оформления: «🌇 Закат» открывается только наградой.
        "themes": [
            {"key": theme, "unlocked": mvp.theme_is_unlocked(theme, owned)}
            for theme in ("dark", "light", "sunset")
        ],
    }


async def _parent_home(user: User) -> dict:
    """Главная родителя: дети, их успехи и ближайшие сроки — без ученических блоков."""
    children = await _parent_children_payload(user)
    nearest: dict | None = None
    for child in children:
        for item in child["deadlines"]:
            if nearest is None or (item["date"] or "") < (nearest["date"] or ""):
                nearest = {**item, "child_name": child["name"]}
    return {
        "role": "parent",
        "children": children,
        "children_count": len(children),
        "completed_week": sum(child["completed_week"] for child in children),
        "overdue_total": sum(child["overdue"] for child in children),
        "next_deadline": nearest["date"] if nearest else None,
        "next_deadline_title": nearest["title"] if nearest else None,
        "next_deadline_child": nearest["child_name"] if nearest else None,
        "notifications_enabled": bool(user.reminders_enabled),
    }


async def _psychologist_home(user: User) -> dict:
    """Главная психолога: сводка сигналов и те, что ждут реакции."""
    summary = await mvp.psychologist_signals_summary(user.id)
    signals = await mvp.psychologist_signals(user.id)
    urgent = [item for item in signals if item.status == "pending"][:5]
    return {
        "role": "psychologist",
        "summary": summary,
        "urgent": [_signal_payload(item) for item in urgent],
        "school": user.school_name,
        "ai_configured": ai_psychologist.is_configured(),
    }


@router.get("/mini/api/home")
async def api_home(request: Request) -> dict:
    user = await _current_user(request)
    role = _role(user)
    if role == "parent":
        return await _parent_home(user)
    if role == "psychologist":
        return await _psychologist_home(user)
    features = _features(user)
    today = datetime.now().isoweekday()  # 1 = понедельник — как в меню бота
    if role == "student":
        lessons = await mvp.schedule_for_day(user, today)
    elif user.class_name:
        lessons = await mvp.schedule_for_class(user.class_name, user.school_name, today)
    else:
        lessons = []
    homework = await mvp.active_homework(user)
    deadlines = await mvp.upcoming_deadlines(user)
    mood_today = None
    if "stress" in features:
        for entry in await mvp.mood_history(user.id, days=1):
            if entry.date == datetime.now().date():
                mood_today = entry.score
    return {
        "role": role,
        "weekday": today,
        "lessons": [_lesson_payload(item, user) for item in lessons],
        "homework_total": len(homework),
        "homework_done": sum(1 for _item, done in homework if done),
        "deadlines_total": len(deadlines),
        "next_deadline": _fmt_dt(deadlines[0].date) if deadlines else None,
        "next_deadline_title": deadlines[0].title if deadlines else None,
        "mood_today": mood_today,
        "points": user.points,
    }


# --------------------------------------------------------------------------- #
# Домашка
# --------------------------------------------------------------------------- #
@router.get("/mini/api/homework")
async def api_homework(request: Request) -> dict:
    user = await _current_user(request)
    _require_feature(user, "homework")
    items = await mvp.active_homework(user)
    return {
        "items": [
            {
                "id": item.id,
                "subject": item.subject,
                "description": item.description,
                "deadline": _fmt_dt(item.deadline),
                "class_name": item.class_name,
                "done": done,
                "mine": item.user_id == user.id,
                "points": item.points,
            }
            for item, done in items
        ]
    }


class HomeworkIn(BaseModel):
    class_name: str
    subject: str
    description: str
    deadline: str
    points: int = 10


@router.post("/mini/api/homework")
async def api_homework_create(request: Request, data: HomeworkIn) -> dict:
    user = await _current_user(request)
    _require_role(user, "teacher")
    if not data.class_name.strip() or not data.subject.strip() or not data.description.strip():
        raise HTTPException(status_code=422, detail="Заполните класс, предмет и задание.")
    if data.points not in range(0, 101):
        raise HTTPException(status_code=422, detail="Баллы должны быть от 0 до 100.")
    item = await mvp.create_homework(
        user.id, user.id,
        subject=data.subject.strip(),
        description=data.description.strip(),
        deadline=_parse_dt(data.deadline),
        class_name=data.class_name.strip(),
        points=data.points,
    )
    return {"ok": True, "id": item.id, "points": item.points}


@router.get("/mini/api/homework/{homework_id}/roster")
async def api_homework_roster(request: Request, homework_id: int) -> dict:
    """Список учеников с отметками сдачи — только для учителя-автора задания."""
    user = await _current_user(request)
    _require_role(user, "teacher")
    roster = await mvp.homework_roster(user, homework_id)
    if roster is None:
        raise HTTPException(status_code=404, detail="Задание не найдено.")
    return {
        "points": await mvp.assignment_points(user, homework_id) or 0,
        "students": [
            {"id": student.id, "name": student.name, "done": done}
            for student, done in roster
        ],
    }


class MarkIn(BaseModel):
    student_id: int
    done: bool


@router.post("/mini/api/homework/{homework_id}/mark")
async def api_homework_mark(request: Request, homework_id: int, data: MarkIn) -> dict:
    """Отметить сдачу ученику может только учительница (автор задания)."""
    user = await _current_user(request)
    _require_role(user, "teacher")
    ok = await mvp.set_homework_completion(user, homework_id, data.student_id, data.done)
    if not ok:
        raise HTTPException(status_code=403, detail="Нет права отмечать это задание.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Дедлайны
# --------------------------------------------------------------------------- #
@router.get("/mini/api/deadlines")
async def api_deadlines(request: Request) -> dict:
    user = await _current_user(request)
    _require_feature(user, "deadlines")
    # Родитель видит сроки своих детей, ученик и учитель — свои и класса.
    if _role(user) == "parent":
        items = await mvp.deadlines_for_parent(user)
    else:
        items = await mvp.upcoming_deadlines(user)
    return {
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "kind": item.kind,
                "date": _fmt_dt(item.date),
                "class_name": item.class_name,
            }
            for item in items
        ]
    }


class DeadlineIn(BaseModel):
    class_name: str = ""
    title: str
    date: str
    kind: str = "контрольная"


@router.post("/mini/api/deadlines")
async def api_deadline_create(request: Request, data: DeadlineIn) -> dict:
    user = await _current_user(request)
    _require_role(user, "teacher")
    if not data.title.strip():
        raise HTTPException(status_code=422, detail="Введите название.")
    if data.kind not in {"контрольная", "самостоятельная", "проект", "зачёт"}:
        raise HTTPException(status_code=422, detail="Неизвестный тип дедлайна.")
    parsed_due = _parse_dt(data.date)
    class_name = data.class_name.strip() or None
    item = await mvp.create_deadline(
        user.id, user.id,
        title=data.title.strip(),
        due=parsed_due,
        kind=data.kind,
        class_name=class_name,
    )
    if class_name:
        from app.main import _notify_parents_about_deadline

        try:
            await _notify_parents_about_deadline(class_name, user, item, data.kind, parsed_due)
        except Exception:
            logger.exception("Failed to notify parents about deadline created via mini app")
    return {"ok": True, "id": item.id}


# --------------------------------------------------------------------------- #
# Расписание: ученик смотрит, учитель редактирует
# --------------------------------------------------------------------------- #
WEEKDAYS = range(1, 8)  # 1 = понедельник … 7 = воскресенье (как в меню бота)


@router.get("/mini/api/schedule")
async def api_schedule(
    request: Request,
    day: int = Query(default=None, ge=1, le=7),
    class_name: str = Query(default=None),
) -> dict:
    user = await _current_user(request)
    _require_feature(user, "schedule")
    role = _role(user)
    weekdays = WEEKDAYS if day is None else [day]
    days: dict[str, list] = {str(item): [] for item in WEEKDAYS}
    editable = "schedule_edit" in _features(user)

    if role == "student":
        for weekday in weekdays:
            items = await mvp.schedule_for_day(user, weekday)
            days[str(weekday)] = [_lesson_payload(item, user) for item in items]
        return {
            "role": role,
            "days": days,
            "class_name": user.class_name,
            "editable": False,
            "classes": [],
        }

    # Учитель: расписание выбранного класса (или первого своего).
    owned = [item.name for item in await crud.classes_for_teacher(user.id)]
    target = (class_name or "").strip()
    if target and target not in owned:
        raise HTTPException(status_code=403, detail="Это не ваш класс.")
    if not target:
        target = owned[0] if owned else (user.class_name or "")
    if not target:
        return {
            "role": role,
            "days": days,
            "class_name": None,
            "editable": editable,
            "classes": owned,
        }
    for weekday in weekdays:
        items = await mvp.schedule_for_class(target, user.school_name, weekday)
        days[str(weekday)] = [_lesson_payload(item, user) for item in items]
    return {
        "role": role,
        "days": days,
        "class_name": target,
        "editable": editable,
        "classes": owned,
    }


class LessonIn(BaseModel):
    class_name: str | None = None
    day: int | str
    time: str
    subject: str
    room: str | None = None


class LessonPatch(BaseModel):
    day: int | str | None = None
    time: str | None = None
    subject: str | None = None
    room: str | None = None


async def _teacher_class(user: User, requested: str | None) -> str:
    """Класс для нового урока: проверяем, что он принадлежит учителю."""
    owned = [item.name for item in await crud.classes_for_teacher(user.id)]
    target = (requested or "").strip()
    if not target:
        if len(owned) == 1:
            return owned[0]
        raise HTTPException(status_code=422, detail="Выберите класс.")
    if target not in owned:
        raise HTTPException(status_code=403, detail="Это не ваш класс.")
    return target


@router.post("/mini/api/schedule")
async def api_schedule_create(request: Request, data: LessonIn) -> dict:
    """Учитель добавляет урок в расписание класса прямо из мини-приложения."""
    user = await _current_user(request)
    _require_feature(user, "schedule_edit")
    subject = (data.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=422, detail="Введите предмет.")
    target = await _teacher_class(user, data.class_name)
    item = await mvp.create_user_schedule_for_class(
        user,
        target,
        _day_number(data.day),
        _parse_lesson_time(data.time),
        subject,
        (data.room or "").strip() or None,
    )
    return {"ok": True, "id": item.id}


@router.patch("/mini/api/schedule/{item_id}")
async def api_schedule_update(request: Request, item_id: int, data: LessonPatch) -> dict:
    """Изменить урок (день, время, предмет, кабинет) может его автор."""
    user = await _current_user(request)
    _require_feature(user, "schedule_edit")
    subject = data.subject.strip() if data.subject is not None else None
    if subject is not None and not subject:
        raise HTTPException(status_code=422, detail="Введите предмет.")
    room = data.room.strip() if data.room is not None else None
    updated = await mvp.update_schedule_item(
        user.id,
        item_id,
        day_of_week=_day_number(data.day) if data.day is not None else None,
        lesson_time=_parse_lesson_time(data.time) if data.time else None,
        subject=subject,
        room=room,
    )
    if updated is None:
        raise HTTPException(
            status_code=404, detail="Урок не найден или создан другим учителем."
        )
    return {"ok": True, "item": _lesson_payload(updated, user)}


@router.delete("/mini/api/schedule/{item_id}")
async def api_schedule_delete(request: Request, item_id: int) -> dict:
    """Удалить урок может только учитель-автор."""
    user = await _current_user(request)
    _require_feature(user, "schedule_edit")
    if not await mvp.delete_schedule_item(user.id, item_id):
        raise HTTPException(
            status_code=404, detail="Урок не найден или создан другим учителем."
        )
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Объявления
# --------------------------------------------------------------------------- #
@router.get("/mini/api/announcements")
async def api_announcements(request: Request) -> dict:
    user = await _current_user(request)
    _require_feature(user, "announcements")
    if _role(user) == "teacher":
        async with async_session() as session:
            rows = await session.scalars(
                select(Announcement)
                .where(Announcement.teacher_id == user.id)
                .order_by(Announcement.created_at.desc())
                .limit(30)
            )
            items = list(rows)
    else:
        items = await mvp.student_announcements(user)
    return {
        "items": [
            {
                "id": item.id,
                "class_name": item.class_name,
                "text": item.text,
                "created_at": item.created_at.strftime("%Y-%m-%dT%H:%M") if item.created_at else None,
            }
            for item in items
        ]
    }


class AnnouncementIn(BaseModel):
    class_name: str
    text: str


@router.post("/mini/api/announcements")
async def api_announcement_create(request: Request, data: AnnouncementIn) -> dict:
    user = await _current_user(request)
    _require_role(user, "teacher")
    class_name = data.class_name.strip()
    text = data.text.strip()
    if not class_name or not text:
        raise HTTPException(status_code=422, detail="Укажите класс и текст объявления.")
    await mvp.create_announcement(user.id, class_name, text)
    # Пуш-уведомление ученикам класса (как в боте).
    recipients = await mvp.announcement_recipients(user.id, class_name)
    sent = 0
    if BOT_TOKEN:
        bot = Bot(token=BOT_TOKEN)
        for recipient in recipients:
            try:
                await bot.send_message(
                    user_id=int(recipient.max_user_id),
                    text=f"📢 Объявление для класса {class_name}:\n{text}",
                )
                sent += 1
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось доставить объявление %s", recipient.max_user_id)
    return {"ok": True, "recipients": len(recipients), "sent": sent}


# --------------------------------------------------------------------------- #
# Антистресс: настроение и чат с ИИ-психологом
# --------------------------------------------------------------------------- #
@router.get("/mini/api/mood")
async def api_mood(request: Request) -> dict:
    user = await _current_user(request)
    _require_feature(user, "stress")
    history = await mvp.mood_history(user.id, days=14)
    return {
        "history": [
            {"date": entry.date.strftime("%Y-%m-%d"), "score": entry.score, "comment": entry.comment}
            for entry in history
        ]
    }


class MoodIn(BaseModel):
    score: int
    comment: str | None = None


@router.post("/mini/api/mood")
async def api_mood_save(request: Request, data: MoodIn) -> dict:
    user = await _current_user(request)
    _require_feature(user, "stress")
    if data.score not in range(1, 6):
        raise HTTPException(status_code=422, detail="Оценка настроения — от 1 до 5.")
    saved = await mvp.save_mood(user, data.score, data.comment)
    if not saved:
        return {"ok": False, "reason": "Настроение за сегодня уже отмечено."}
    return {"ok": True, "points": 3}


class ChatIn(BaseModel):
    message: str


@router.post("/mini/api/chat")
async def api_chat(request: Request, data: ChatIn) -> dict:
    user = await _current_user(request)
    _require_feature(user, "stress")
    value = data.message.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Пустое сообщение.")
    score, _themes = mvp.analyze_stress(value)
    if score >= 5:
        # Кризисное сообщение тоже сохраняем: иначе сигнал психологу получит
        # пустую историю и неверный уровень риска.
        await mvp.record_chat_message(user, value)
        return {"reply": AI_CRISIS_REPLY, "crisis": True, "needs_human": True, "score": score}
    reply = await ai_psychologist.ask(value, str(user.max_user_id))
    reply, needs_human = ai_psychologist.pop_human_marker(reply)
    # История переписки хранится на сервере: по ней строится сигнал психологу
    # (риск и темы), поэтому мини-приложение сохраняет сообщения так же, как бот.
    await mvp.record_chat_message(user, value)
    if score >= 4:
        reply += (
            "\n\nЕсли станет совсем тяжело — я могу анонимно передать сигнал "
            "школьному психологу, только с твоего согласия."
        )
    return {
        "reply": reply,
        "crisis": False,
        "needs_human": needs_human or score >= 4,
        "score": score,
    }


# --------------------------------------------------------------------------- #
# Классы: вход ученика и список учителя
# --------------------------------------------------------------------------- #
class JoinClassIn(BaseModel):
    login: str
    password: str


@router.post("/mini/api/join_class")
async def api_join_class(request: Request, data: JoinClassIn) -> dict:
    user = await _current_user(request)
    _require_role(user, "student")
    try:
        message = await crud.join_school_class(
            user.id, data.login.strip(), data.password.strip()
        )
    except (ValueError, PermissionError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"ok": True, "message": message}


@router.get("/mini/api/classes")
async def api_classes(request: Request) -> dict:
    user = await _current_user(request)
    _require_role(user, "teacher")
    classes = await crud.classes_for_teacher(user.id)
    items = []
    async with async_session() as session:
        for school_class in classes:
            count = await session.scalar(
                select(func.count(ClassMember.id)).where(
                    ClassMember.class_id == school_class.id
                )
            )
            try:
                decrypted = crud.decrypt_class_password(school_class.password_encrypted)
            except Exception:
                decrypted = "—"
            items.append(
                {
                    "id": school_class.id,
                    "name": school_class.name,
                    "login": school_class.login,
                    "password": decrypted,
                    "members": int(count or 0),
                }
            )
    return {"items": items}


# --------------------------------------------------------------------------- #
# Профиль, награды и статистика
# --------------------------------------------------------------------------- #
@router.get("/mini/api/profile")
async def api_profile(request: Request) -> dict:
    user = await _current_user(request)
    _require_feature(user, "profile")
    owned = await mvp.user_reward_keys(user.id)
    catalog = []
    for key, (title, cost, description) in mvp.REWARD_CATALOG.items():
        catalog.append(
            {"key": key, "title": title, "cost": cost,
             "description": description, "owned": key in owned}
        )
    return {
        "name": user.full_name,
        "role": _role(user),
        "class_name": user.class_name,
        "school": user.school_name,
        "grade": user.grade,
        "points": user.points,
        "rewards": catalog,
    }


class RewardIn(BaseModel):
    key: str


@router.post("/mini/api/rewards")
async def api_reward_unlock(request: Request, data: RewardIn) -> dict:
    user = await _current_user(request)
    _require_feature(user, "profile")
    entry = mvp.REWARD_CATALOG.get(data.key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Такой награды нет.")
    _title, cost, _desc = entry
    ok = await mvp.unlock_reward(user, data.key, cost)
    if not ok:
        raise HTTPException(status_code=409, detail="Недостаточно очков или уже получено.")
    return {"ok": True}


@router.get("/mini/api/stats")
async def api_stats(request: Request) -> dict:
    user = await _current_user(request)
    _require_role(user, "teacher")
    done, total = await mvp.today_counts_for_teacher(user)
    report = await mvp.teacher_completion_report(user)
    return {
        "today_done": done,
        "today_total": total,
        "classes": [
            {"class_name": name, "done": finished, "total": all_count}
            for name, finished, all_count in report
        ],
    }


# --------------------------------------------------------------------------- #
# Родитель: дети и учебная сводка
# --------------------------------------------------------------------------- #
@router.get("/mini/api/children")
async def api_children(request: Request) -> dict:
    """Дети родителя: успехи за неделю, просрочки и ближайшие сроки."""
    user = await _current_user(request)
    _require_feature(user, "children")
    return {
        "items": await _parent_children_payload(user),
        "notifications_enabled": bool(user.reminders_enabled),
    }


@router.get("/mini/api/parent/summary")
async def api_parent_summary(
    request: Request, child_id: int = Query(default=None)
) -> dict:
    """Развернутая сводка по одному ребёнку (по умолчанию — первый)."""
    user = await _current_user(request)
    _require_feature(user, "summary")
    student, completed, overdue, deadlines = await mvp.parent_summary(user, child_id)
    if student is None:
        return {"child": None, "completed_week": 0, "overdue": 0, "deadlines": []}
    return {
        "child": {
            "id": student.id,
            "name": student.full_name,
            "class_name": student.class_name,
            "grade": student.grade,
            "school": student.school_name,
        },
        "completed_week": completed,
        "overdue": overdue,
        "deadlines": [
            {"id": item.id, "title": item.title, "kind": item.kind,
             "class_name": item.class_name, "date": _fmt_dt(item.date)}
            for item in deadlines
        ],
    }


PARENT_TIPS: tuple[str, ...] = (
    "Спрашивайте «как прошёл день?», а не «что получил?» — так ребёнок охотнее делится.",
    "Хвалите за усилия и регулярность, а не только за результат.",
    "Дедлайн ближе, чем кажется: предложите помощь с планом, но не делайте задание вместо ребёнка.",
    "Если ребёнок замкнулся или плохо спит — это повод обратиться к школьному психологу.",
    "Договоритесь о времени без телефона перед сном: сон сильнее влияет на оценки, чем ещё час занятий.",
)


@router.get("/mini/api/parent/tips")
async def api_parent_tips(request: Request) -> dict:
    """Короткие советы по поддержке подростка (как в меню родителя бота)."""
    user = await _current_user(request)
    _require_feature(user, "tips")
    return {"items": list(PARENT_TIPS)}


# --------------------------------------------------------------------------- #
# Ученик: связь с живым психологом
# --------------------------------------------------------------------------- #
SIGNAL_FORMATS = ("chat", "live")


@router.get("/mini/api/psychologist")
async def api_psychologist_info(request: Request) -> dict:
    """Доступен ли ученику живой психолог и какие школы можно выбрать.

    Без этого экрана ученик в мини-приложении упирался в подсказку «напиши боту»:
    передать сигнал живому психологу отсюда было нельзя.
    """
    user = await _current_user(request)
    _require_feature(user, "psychologist_signal")
    school = (user.school_name or "").strip()
    psychologist = await mvp.psychologist_for_school(school) if school else None
    schools = await mvp.psychologist_schools()
    duty = None if psychologist is not None else await mvp.any_psychologist()
    return {
        "school": user.school_name,
        "school_set": bool(school),
        "available": psychologist is not None,
        "duty_available": duty is not None,
        "schools": schools,
        "crisis": AI_CRISIS_REPLY,
    }


class SignalIn(BaseModel):
    preferred_format: str = "chat"
    school: str | None = None
    allow_duty: bool = False


@router.post("/mini/api/signals")
async def api_signal_create(request: Request, data: SignalIn) -> dict:
    """Передать анонимный сигнал школьному психологу (только с согласия ученика)."""
    user = await _current_user(request)
    _require_feature(user, "psychologist_signal")
    if data.preferred_format not in SIGNAL_FORMATS:
        raise HTTPException(status_code=422, detail="Формат связи: chat или live.")
    school = (data.school or user.school_name or "").strip()
    if not school:
        return {
            "ok": False,
            "reason": "no_school",
            "schools": await mvp.psychologist_schools(),
        }
    if school != (user.school_name or ""):
        await mvp.update_profile(str(user.max_user_id), school=school[:255])
        user.school_name = school[:255]
    psychologist = await mvp.psychologist_for_school(school)
    duty = False
    if psychologist is None:
        if not data.allow_duty:
            # Психолога этой школы нет — предлагаем дежурного, но не отправляем
            # сигнал без явного согласия ученика.
            return {
                "ok": False,
                "reason": "no_psychologist",
                "schools": await mvp.psychologist_schools(),
                "duty_available": (await mvp.any_psychologist()) is not None,
            }
        psychologist = await mvp.any_psychologist()
        duty = True
    if psychologist is None:
        return {"ok": False, "reason": "none", "crisis": AI_CRISIS_REPLY}
    if await mvp.has_open_signal(user.id, psychologist.id):
        # Сигнал уже в работе — повторная отправка только засоряет очередь.
        return {
            "ok": True,
            "already_open": True,
            "crisis": AI_CRISIS_REPLY,
        }
    risk_level, themes = await mvp.stress_snapshot(user)
    signal = await mvp.create_stress_signal(
        user.id,
        psychologist.id,
        risk_level=risk_level,
        themes=themes,
        preferred_format=data.preferred_format,
        consent=True,
    )
    if BOT_TOKEN:
        format_label = "личная встреча" if data.preferred_format == "live" else "переписка"
        try:
            await Bot(token=BOT_TOKEN).send_message(
                user_id=int(psychologist.max_user_id),
                text=(
                    f"📥 Новый сигнал №{signal.id} (риск {signal.risk_level}/5)\n"
                    f"Ученик: {user.full_name}, класс {user.class_name or '—'}, "
                    f"школа: {user.school_name or '—'}\n"
                    f"Темы: {signal.themes or 'не указаны'}\n"
                    f"Формат связи: {format_label}"
                ),
            )
        except Exception:  # noqa: BLE001 - уведомление не должно ломать сигнал
            logger.exception(
                "Could not notify psychologist %s about mini-app signal",
                psychologist.max_user_id,
            )
    return {"ok": True, "id": signal.id, "duty": duty, "crisis": AI_CRISIS_REPLY}



# --------------------------------------------------------------------------- #
# Психолог: сигналы учеников
# --------------------------------------------------------------------------- #
@router.get("/mini/api/signals")
async def api_signals(request: Request) -> dict:
    """Сигналы учеников школы: сводка и список (только психолог)."""
    user = await _current_user(request)
    _require_feature(user, "signals")
    signals = await mvp.psychologist_signals(user.id)
    return {
        "summary": await mvp.psychologist_signals_summary(user.id),
        "items": [_signal_payload(item) for item in signals],
        "statuses": [
            {"key": key, "label": SIGNAL_STATUS_LABELS[key]} for key in SIGNAL_STATUSES
        ],
    }


class SignalStatusIn(BaseModel):
    status: str


@router.post("/mini/api/signals/{signal_id}")
async def api_signal_status(
    request: Request, signal_id: int, data: SignalStatusIn
) -> dict:
    """Сменить статус сигнала: pending → in_progress → closed."""
    user = await _current_user(request)
    _require_feature(user, "signal_status")
    if data.status not in SIGNAL_STATUSES:
        raise HTTPException(status_code=422, detail="Неизвестный статус сигнала.")
    signal = await mvp.psychologist_signal(user.id, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Сигнал не найден.")
    await mvp.update_signal_status(signal_id, data.status)
    return {
        "ok": True,
        "status": data.status,
        "status_label": SIGNAL_STATUS_LABELS[data.status],
    }


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #
class SettingsIn(BaseModel):
    reminders_enabled: bool | None = None
    notify_7d: bool | None = None
    notify_3d: bool | None = None
    notify_1d: bool | None = None
    # Профиль: школа нужна ученику, чтобы найти своего психолога,
    # класс — чтобы видеть задания и расписание своего класса.
    school: str | None = None
    class_name: str | None = None


@router.post("/mini/api/settings")
async def api_settings(request: Request, data: SettingsIn) -> dict:
    user = await _current_user(request)
    values = {k: v for k, v in data.model_dump().items() if v is not None}
    school = values.get("school")
    if school is not None:
        values["school"] = school.strip()[:255]
    class_name = values.get("class_name")
    if class_name is not None:
        values["class_name"] = class_name.strip()[:64]
    if values:
        await mvp.update_profile(str(user.max_user_id), **values)
    return {"ok": True}
