"""Тесты фонового планировщика напоминаний (app/services/scheduler.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import scheduler


def make_user(role: str = "student", **kwargs: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": 1,
        "max_user_id": "42",
        "role": role,
        "reminders_enabled": True,
        "mood_survey_hour": None,
        "notify_7d": True,
        "notify_3d": True,
        "notify_1d": True,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def patch_mvp(monkeypatch: pytest.MonkeyPatch, **fakes: object) -> None:
    for name, fake in fakes.items():
        monkeypatch.setattr(scheduler.mvp, name, fake)


NOW = datetime(2026, 10, 21, 12, 0)


def test_survey_hour_is_clamped_and_defaulted() -> None:
    assert scheduler.survey_hour_for(SimpleNamespace(mood_survey_hour=None)) == 18
    assert scheduler.survey_hour_for(SimpleNamespace(mood_survey_hour=3)) == 8
    assert scheduler.survey_hour_for(SimpleNamespace(mood_survey_hour=99)) == 22
    assert scheduler.survey_hour_for(SimpleNamespace()) == 18


def test_mood_survey_respects_configured_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(mood_survey_hour=10)

    async def mood_was_logged_today(user_id: int) -> bool:
        return False

    async def upcoming_deadlines(target: object) -> list[object]:
        return []

    async def active_homework(target: object) -> list[tuple[object, bool]]:
        return []

    patch_mvp(
        monkeypatch,
        mood_was_logged_today=mood_was_logged_today,
        upcoming_deadlines=upcoming_deadlines,
        active_homework=active_homework,
    )

    early = asyncio.run(
        scheduler.build_student_reminders(user, NOW.replace(hour=9))
    )
    assert early == []

    late = asyncio.run(
        scheduler.build_student_reminders(user, NOW.replace(hour=10, minute=30))
    )
    assert [key for key, _ in late] == ["mood:2026-10-21"]
    assert "Как ты сегодня" in late[0][1]


def test_mood_survey_skipped_when_already_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(mood_survey_hour=10)

    async def mood_was_logged_today(user_id: int) -> bool:
        return True

    async def upcoming_deadlines(target: object) -> list[object]:
        return []

    async def active_homework(target: object) -> list[tuple[object, bool]]:
        return []

    patch_mvp(
        monkeypatch,
        mood_was_logged_today=mood_was_logged_today,
        upcoming_deadlines=upcoming_deadlines,
        active_homework=active_homework,
    )

    reminders = asyncio.run(
        scheduler.build_student_reminders(user, NOW.replace(hour=11))
    )
    assert reminders == []


def test_student_reminders_for_notify_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """напоминания за неделю, за 3 дня и за день до срока."""
    user = make_user()
    deadline_1d = SimpleNamespace(
        id=5,
        title="КР по алгебре",
        kind="контрольная",
        date=NOW + timedelta(days=1, hours=2),
    )
    deadline_3d = SimpleNamespace(
        id=6, title="Проект", kind="проект", date=NOW + timedelta(days=3)
    )
    deadline_7d = SimpleNamespace(
        id=7, title="Зачёт", kind="зачёт", date=NOW + timedelta(days=7)
    )
    deadline_2d = SimpleNamespace(
        id=8, title="Лишнее", kind="контрольная", date=NOW + timedelta(days=2)
    )
    homework_today = SimpleNamespace(
        id=9, subject="Алгебра", description="§12", deadline=NOW + timedelta(hours=6)
    )
    homework_done = SimpleNamespace(
        id=10, subject="Физика", description="§3", deadline=NOW + timedelta(days=1)
    )
    homework_far = SimpleNamespace(
        id=11, subject="История", description="§9", deadline=NOW + timedelta(days=5)
    )

    async def mood_was_logged_today(user_id: int) -> bool:
        return True

    async def upcoming_deadlines(target: object) -> list[object]:
        return [deadline_1d, deadline_3d, deadline_7d, deadline_2d]

    async def active_homework(target: object) -> list[tuple[object, bool]]:
        return [(homework_today, False), (homework_done, True), (homework_far, False)]

    patch_mvp(
        monkeypatch,
        mood_was_logged_today=mood_was_logged_today,
        upcoming_deadlines=upcoming_deadlines,
        active_homework=active_homework,
    )

    reminders = asyncio.run(scheduler.build_student_reminders(user, NOW))
    keys = [key for key, _ in reminders]
    # Событие через 2 дня и выполненная/дальняя домашка не напоминаются.
    assert keys == [
        "deadline:5:1d",
        "deadline:6:3d",
        "deadline:7:7d",
        "homework:9:0d",
    ]
    assert "завтра" in reminders[0][1]
    assert "КР по алгебре" in reminders[0][1]
    assert "через 3 дня" in reminders[1][1]
    assert "через неделю" in reminders[2][1]
    assert "сегодня" in reminders[3][1]
    assert "§12" in reminders[3][1]


def test_student_reminders_respect_disabled_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отключённые в настройках сроки не приходят (notify_7d/notify_1d)."""
    user = make_user(notify_7d=False, notify_1d=False)
    deadline_1d = SimpleNamespace(
        id=5, title="КР", kind="контрольная", date=NOW + timedelta(days=1)
    )
    deadline_3d = SimpleNamespace(
        id=6, title="Проект", kind="проект", date=NOW + timedelta(days=3)
    )
    deadline_7d = SimpleNamespace(
        id=7, title="Зачёт", kind="зачёт", date=NOW + timedelta(days=7)
    )

    async def mood_was_logged_today(user_id: int) -> bool:
        return True

    async def upcoming_deadlines(target: object) -> list[object]:
        return [deadline_1d, deadline_3d, deadline_7d]

    async def active_homework(target: object) -> list[tuple[object, bool]]:
        return []

    patch_mvp(
        monkeypatch,
        mood_was_logged_today=mood_was_logged_today,
        upcoming_deadlines=upcoming_deadlines,
        active_homework=active_homework,
    )

    reminders = asyncio.run(scheduler.build_student_reminders(user, NOW))
    assert [key for key, _ in reminders] == ["deadline:6:3d"]


def test_parent_reminders_only_for_notify_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Родители получают события ребёнка за неделю, 3 дня и 1 день."""
    user = make_user(role="parent")
    week = SimpleNamespace(
        id=1, title="Контрольная", kind="контрольная", date=NOW + timedelta(days=7)
    )
    three = SimpleNamespace(
        id=2, title="Проект", kind="проект", date=NOW + timedelta(days=3)
    )
    one = SimpleNamespace(
        id=3, title="Зачёт", kind="зачёт", date=NOW + timedelta(days=1)
    )
    two = SimpleNamespace(
        id=4, title="Лишнее", kind="контрольная", date=NOW + timedelta(days=2)
    )
    past = SimpleNamespace(
        id=5, title="Старое", kind="зачёт", date=NOW - timedelta(days=1)
    )

    async def deadlines_for_parent(target: object) -> list[object]:
        return [week, three, one, two, past]

    patch_mvp(monkeypatch, deadlines_for_parent=deadlines_for_parent)

    reminders = asyncio.run(scheduler.build_parent_reminders(user, NOW))
    assert [key for key, _ in reminders] == [
        "parent_deadline:1:7d",
        "parent_deadline:2:3d",
        "parent_deadline:3:1d",
    ]
    assert "Учебное событие у ребёнка" in reminders[0][1]
    assert "через неделю" in reminders[0][1]


def test_run_once_sends_and_records_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(role="parent")
    sent: list[tuple[int, str]] = []
    recorded: list[tuple[int, str]] = []
    event = SimpleNamespace(
        id=1, title="Контрольная", kind="контрольная", date=NOW + timedelta(days=1)
    )

    async def reminder_users() -> list[object]:
        return [user]

    async def deadlines_for_parent(target: object) -> list[object]:
        return [event]

    async def reminder_already_delivered(user_id: int, delivery_key: str) -> bool:
        return False

    async def record_reminder_delivery(user_id: int, delivery_key: str) -> None:
        recorded.append((user_id, delivery_key))

    async def sender(max_user_id: int, text: str) -> None:
        sent.append((max_user_id, text))

    patch_mvp(
        monkeypatch,
        reminder_users=reminder_users,
        deadlines_for_parent=deadlines_for_parent,
        reminder_already_delivered=reminder_already_delivered,
        record_reminder_delivery=record_reminder_delivery,
    )

    count = asyncio.run(scheduler.run_once(sender, now=NOW))

    assert count == 1
    assert sent == [(42, sent[0][1])]
    assert "Контрольная" in sent[0][1]
    assert recorded == [(1, "parent_deadline:1:1d")]


def test_run_once_skips_already_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(role="parent")
    sent: list[tuple[int, str]] = []
    event = SimpleNamespace(
        id=1, title="Контрольная", kind="контрольная", date=NOW + timedelta(days=1)
    )

    async def reminder_users() -> list[object]:
        return [user]

    async def deadlines_for_parent(target: object) -> list[object]:
        return [event]

    async def reminder_already_delivered(user_id: int, delivery_key: str) -> bool:
        return True

    async def record_reminder_delivery(user_id: int, delivery_key: str) -> None:
        raise AssertionError("не должно вызываться")

    async def sender(max_user_id: int, text: str) -> None:
        sent.append((max_user_id, text))

    patch_mvp(
        monkeypatch,
        reminder_users=reminder_users,
        deadlines_for_parent=deadlines_for_parent,
        reminder_already_delivered=reminder_already_delivered,
        record_reminder_delivery=record_reminder_delivery,
    )

    count = asyncio.run(scheduler.run_once(sender, now=NOW))

    assert count == 0
    assert sent == []


def test_run_once_ignores_teacher_role(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(role="teacher")
    sent: list[tuple[int, str]] = []

    async def reminder_users() -> list[object]:
        return [user]

    async def sender(max_user_id: int, text: str) -> None:
        sent.append((max_user_id, text))

    patch_mvp(monkeypatch, reminder_users=reminder_users)

    count = asyncio.run(scheduler.run_once(sender, now=NOW))

    assert count == 0
    assert sent == []

SATURDAY = datetime(2026, 10, 24, 12, 0)  # NOW (среда) + 3 дня


def _patch_study_items(monkeypatch: pytest.MonkeyPatch, anchor: datetime) -> None:
    """Один дедлайн и одна домашка со сроком «завтра» относительно anchor."""
    deadline = SimpleNamespace(
        id=3,
        title="Контрольная",
        kind="контрольная",
        date=anchor + timedelta(days=1),
    )
    homework = SimpleNamespace(
        id=4,
        subject="Алгебра",
        description="§12",
        deadline=anchor + timedelta(days=1),
    )

    async def mood_was_logged_today(user_id: int) -> bool:
        return True

    async def upcoming_deadlines(target: object) -> list[object]:
        return [deadline]

    async def active_homework(target: object) -> list[tuple[object, bool]]:
        return [(homework, False)]

    patch_mvp(
        monkeypatch,
        mood_was_logged_today=mood_was_logged_today,
        upcoming_deadlines=upcoming_deadlines,
        active_homework=active_homework,
    )


def test_study_reminders_are_not_silenced_on_weekends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Учебные напоминания приходят и в выходные — «тихих» наград больше нет."""
    user = make_user(mood_survey_hour=10)
    _patch_study_items(monkeypatch, NOW)

    weekday = asyncio.run(scheduler.build_student_reminders(user, NOW))
    assert [key for key, _ in weekday] == ["deadline:3:1d", "homework:4:1d"]

    # Суббота — напоминания работают так же, как в будни.
    _patch_study_items(monkeypatch, SATURDAY)
    assert len(asyncio.run(scheduler.build_student_reminders(user, SATURDAY))) == 2


def test_parent_reminders_are_not_silenced_on_weekends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = make_user(role="parent")
    deadline = SimpleNamespace(
        id=8,
        title="Собрание",
        kind="событие",
        date=SATURDAY + timedelta(days=1),
    )

    async def deadlines_for_parent(target: object) -> list[object]:
        return [deadline]

    patch_mvp(monkeypatch, deadlines_for_parent=deadlines_for_parent)
    assert len(asyncio.run(scheduler.build_parent_reminders(parent, SATURDAY))) == 1
