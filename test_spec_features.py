"""Проверки соответствия онбординг, расписание, карточки,
награды, время опроса, уведомления родителей, кабинет психолога."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest
from maxapi import Bot
from maxapi.enums.chat_type import ChatType
from maxapi.types import CallbackButton, Message, MessageCallback, Recipient
from maxapi.types.callback import Callback
from maxapi.types.users import User as MaxUser

from app import main
from app.db.models import User
from app.keyboards.inline import (
    ButtonsPayload,
    registration_roles_keyboard,
    staff_roles_keyboard,
)


def callback_event(payload: str, bot: Bot) -> MessageCallback:
    event = MessageCallback(
        timestamp=1,
        message=Message(
            recipient=Recipient(chat_id=10, chat_type=ChatType.DIALOG),
            timestamp=1,
        ),
        callback=Callback(
            timestamp=1,
            callback_id="callback-id",
            payload=payload,
            user=MaxUser(user_id=42, first_name="Test", is_bot=False),
        ),
    )
    event.bot = bot
    return event


def make_bot(
    monkeypatch: pytest.MonkeyPatch,
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]],
    user: User,
) -> Bot:
    """Бот с перехватом _send, ack и поиска пользователя."""
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)
    return bot


def payloads_of(buttons: ButtonsPayload | None) -> list[str]:
    assert buttons is not None
    return [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]


# ---------------------------------------------------------------------------
# Стартовое окно: ученик / сотрудник школы / родитель + уточнение сотрудника
# ---------------------------------------------------------------------------


def test_registration_keyboard_offers_three_roles() -> None:
    payloads = payloads_of(registration_roles_keyboard())
    assert payloads == [
        "onboard:role:student",
        "onboard:role:staff",
        "onboard:role:parent",
    ]


def test_staff_keyboard_clarifies_employee_role() -> None:
    payloads = payloads_of(staff_roles_keyboard())
    assert set(payloads) == {
        "onboard:role:teacher",
        "onboard:role:psychologist",
        "onboard:role_back",
    }


def test_staff_button_asks_clarification(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="unselected", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("onboard:role:staff", bot)))

    assert "Уточните вашу роль" in sent[0][1]
    assert set(payloads_of(sent[0][2])) == {
        "onboard:role:teacher",
        "onboard:role:psychologist",
        "onboard:role_back",
    }


def test_role_back_returns_to_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="unselected", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("onboard:role_back", bot)))

    assert payloads_of(sent[0][2]) == [
        "onboard:role:student",
        "onboard:role:staff",
        "onboard:role:parent",
    ]


# ---------------------------------------------------------------------------
# Расписание класса: ведёт учитель — ученик только смотрит
# ---------------------------------------------------------------------------


def test_schedule_week_view_lists_all_days(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(
        max_user_id="42", role="student", status="approved", class_name="9А"
    )
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    lesson = SimpleNamespace(
        id=1,
        user_id=5,
        created_by=5,
        time=time(hour=10, minute=0),
        subject="Химия",
        room="12",
    )

    async def schedule_for_day(target: User, weekday: int) -> list[object]:
        return [lesson] if weekday == 3 else []

    monkeypatch.setattr(main.mvp, "schedule_for_day", schedule_for_day)

    asyncio.run(main._handle_callback(callback_event("student:schedule_week", bot)))

    text = sent[0][1]
    assert "Расписание класса 9А на неделю" in text
    assert "Понедельник:" in text
    assert "уроков нет" in text
    assert "Среда:" in text
    assert "10:00 — Химия, каб. 12" in text
    assert "student:schedule" in payloads_of(sent[0][2])


def test_student_cannot_delete_lessons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """расписание класса ведёт учитель — ученик не удаляет уроки."""
    user = User(
        max_user_id="42", role="student", status="approved", class_name="9А"
    )
    user.id = 5
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def delete_schedule_item(user_id: int, item_id: int) -> bool:
        raise AssertionError("ученик не должен удалять уроки")

    monkeypatch.setattr(main.mvp, "delete_schedule_item", delete_schedule_item)

    asyncio.run(
        main._handle_callback(callback_event("student:lesson_del:7:3", bot))
    )

    assert "Ученики не могут удалять уроки" in sent[0][1]
    assert "student:schedule" in payloads_of(sent[0][2])


def test_student_cannot_add_lessons(monkeypatch: pytest.MonkeyPatch) -> None:
    """уроки добавляет только учитель."""
    user = User(
        max_user_id="42", role="student", status="approved", class_name="9А"
    )
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    main._states.pop("42", None)

    asyncio.run(main._handle_callback(callback_event("student:schedule_add", bot)))

    assert "Ученики не могут добавлять уроки" in sent[0][1]
    assert main._states.get("42") is None


def test_schedule_fsm_rejects_student_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FSM добавления урока доступна только учителю."""
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    make_bot(monkeypatch, sent, user)
    steps: list[str | None] = []

    async def set_state(target: User, step: str | None, **data: object) -> None:
        steps.append(step)

    async def create_user_schedule_for_class(*args, **kwargs):
        raise AssertionError("ученик не должен создавать уроки")

    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(
        main.mvp, "create_user_schedule_for_class", create_user_schedule_for_class
    )

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="sch_room",
            text="204",
            state={
                "class_name": "9А",
                "day": 1,
                "lesson_time": time(hour=8, minute=30),
                "subject": "Алгебра",
            },
        )
    )

    assert handled is True
    assert steps == [None]
    assert "только учитель" in sent[0][1]


def test_teacher_class_schedule_shows_lessons_and_delete_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Учитель видит расписание класса; удалять можно только свои уроки."""
    user = User(
        max_user_id="42", role="teacher", status="approved", school="Школа №1"
    )
    user.id = 7
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    items = [
        SimpleNamespace(
            id=1,
            created_by=7,
            day_of_week=1,
            time=time(hour=8, minute=30),
            subject="Алгебра",
            room="204",
        ),
        SimpleNamespace(
            id=2,
            created_by=99,
            day_of_week=3,
            time=time(hour=9, minute=30),
            subject="Физика",
            room=None,
        ),
    ]

    async def schedule_for_class(
        class_name: str, school: str | None, weekday: int | None = None
    ) -> list[object]:
        assert class_name == "9А"
        assert school == "Школа №1"
        assert weekday is None
        return items

    monkeypatch.setattr(main.mvp, "schedule_for_class", schedule_for_class)

    asyncio.run(
        main._handle_callback(callback_event("teacher:schedule:class:9А", bot))
    )

    text = sent[0][1]
    assert "Расписание класса 9А" in text
    assert "Понедельник:" in text
    assert "08:30 — Алгебра, каб. 204" in text
    assert "Среда:" in text
    assert "09:30 — Физика" in text
    payloads = payloads_of(sent[0][2])
    assert "teacher:schedule_add:9А" in payloads
    assert "teacher:lesson_del:1:9А" in payloads
    assert "teacher:lesson_del:2:9А" not in payloads


def test_teacher_lesson_delete_removes_own_lesson(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        max_user_id="42", role="teacher", status="approved", school="Школа №1"
    )
    user.id = 7
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    deleted: list[tuple[int, int]] = []

    async def delete_schedule_item(user_id: int, item_id: int) -> bool:
        deleted.append((user_id, item_id))
        return True

    async def schedule_for_class(
        class_name: str, school: str | None, weekday: int | None = None
    ) -> list[object]:
        return []

    monkeypatch.setattr(main.mvp, "delete_schedule_item", delete_schedule_item)
    monkeypatch.setattr(main.mvp, "schedule_for_class", schedule_for_class)

    asyncio.run(
        main._handle_callback(callback_event("teacher:lesson_del:7:9А", bot))
    )

    assert deleted == [(7, 7)]
    assert "Урок удалён" in sent[0][1]
    assert "Расписание класса 9А" in sent[1][1]
    assert "уроков пока нет" in sent[1][1]


# ---------------------------------------------------------------------------
# Антистресс-карточки на 1–2 минуты
# ---------------------------------------------------------------------------


def test_stress_menu_contains_card_button(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("student:stress", bot)))

    assert "student:stress_card" in payloads_of(sent[0][2])


def test_stress_card_shows_random_short_practice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("student:stress_card", bot)))

    text = sent[0][1]
    assert text.startswith("🃏 Карточка")
    assert ("минута" in text) or ("минуты" in text)
    payloads = payloads_of(sent[0][2])
    assert "student:stress_card" in payloads  # «🔄 Другую карточку»
    assert "menu:student" in payloads


def test_all_stress_cards_are_short() -> None:
    assert len(main.STRESS_CARDS) >= 5
    for title, body in main.STRESS_CARDS:
        assert title.startswith("🃏")
        assert len(body) <= 400



# ---------------------------------------------------------------------------
# Геймификация: кастомизация профиля за очки и список наград
# ---------------------------------------------------------------------------


def test_rewards_list_shows_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved", points=120)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def user_reward_keys(user_id: int) -> set[str]:
        return {"emoji_cosmos"}

    monkeypatch.setattr(main.mvp, "user_reward_keys", user_reward_keys)

    asyncio.run(main._handle_callback(callback_event("student:rewards", bot)))

    text = sent[0][1]
    assert "🎁 Награды и кастомизация профиля" in text
    assert "Твои очки: 120" in text
    assert "✅ 🚀 Стиль профиля «Космос» — открыто" in text
    assert "🔒 🏆 Титул «Чемпион недели» — 50 очков" in text
    payloads = payloads_of(sent[0][2])
    assert "student:reward_buy:title_champion" in payloads
    assert "student:reward_buy:emoji_cosmos" not in payloads


def test_reward_buy_spends_points(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved", points=100)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    purchases: list[tuple[int, str]] = []

    async def user_reward_keys(user_id: int) -> set[str]:
        return {"title_champion"}

    async def unlock_reward(target: User, item_key: str, cost: int) -> bool:
        purchases.append((cost, item_key))
        target.points -= cost
        return True

    monkeypatch.setattr(main.mvp, "user_reward_keys", user_reward_keys)
    monkeypatch.setattr(main.mvp, "unlock_reward", unlock_reward)

    asyncio.run(
        main._handle_callback(callback_event("student:reward_buy:title_champion", bot))
    )

    assert purchases == [(50, "title_champion")]
    assert "🎉 Награда открыта: 🏆 Титул «Чемпион недели»" in sent[0][1]
    assert "Осталось очков: 50" in sent[0][1]
    # После покупки показываем обновлённый список наград.
    assert "✅ 🏆 Титул «Чемпион недели» — открыто" in sent[1][1]


def test_reward_buy_unknown_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(
        main._handle_callback(callback_event("student:reward_buy:unknown", bot))
    )

    assert "Такой награды нет в списке." in sent[0][1]


def test_profile_shows_name_class_school_and_badges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        max_user_id="42",
        role="student",
        status="approved",
        name="Иванов Иван",
        class_name="9А",
        school="Школа №1",
        points=70,
    )
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def user_reward_keys(user_id: int) -> set[str]:
        return {"title_champion"}

    monkeypatch.setattr(main.mvp, "user_reward_keys", user_reward_keys)

    asyncio.run(main._handle_callback(callback_event("student:profile", bot)))

    text = sent[0][1]
    assert "Профиль: Иванов Иван 🏆" in text
    assert "Класс: 9А" in text
    assert "Школа: Школа №1" in text
    assert "Очки: 70" in text
    assert "Титулы и оформление: 🏆 Титул «Чемпион недели»" in text
    assert "student:rewards" in payloads_of(sent[0][2])



# ---------------------------------------------------------------------------
# Настройки: время опроса «Как ты сегодня?»
# ---------------------------------------------------------------------------


def test_student_settings_shows_survey_time(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("student:settings", bot)))

    assert "Время опроса «Как ты сегодня?»: 18:00" in sent[0][1]
    assert "student:survey_time" in payloads_of(sent[0][2])


def test_survey_time_step_saves_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    saved: list[dict[str, object]] = []
    make_bot(monkeypatch, sent, user)

    async def update_profile(max_user_id: str, **values: object) -> User:
        saved.append(values)
        return user

    async def set_state(target: User, step: str | None, **data: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main, "_set_state", set_state)

    handled = asyncio.run(
        main._handle_input_step(10, 42, user, "survey_time", "19", {})
    )

    assert handled is True
    assert saved == [{"mood_survey_hour": 19}]
    assert "в 19:00" in sent[0][1]


def test_survey_time_step_rejects_invalid_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    saved: list[dict[str, object]] = []
    make_bot(monkeypatch, sent, user)

    async def update_profile(max_user_id: str, **values: object) -> User:
        saved.append(values)
        return user

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)

    handled = asyncio.run(
        main._handle_input_step(10, 42, user, "survey_time", "30", {})
    )

    assert handled is True
    assert saved == []
    assert "Введи час от 8 до 22" in sent[0][1]


def test_survey_hour_quick_button(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    saved: list[dict[str, object]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def update_profile(max_user_id: str, **values: object) -> User:
        saved.append(values)
        return user

    async def set_state(target: User, step: str | None, **data: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main, "_set_state", set_state)

    asyncio.run(main._handle_callback(callback_event("student:survey_hour:20", bot)))

    assert saved == [{"mood_survey_hour": 20}]
    assert "в 20:00" in sent[0][1]



# ---------------------------------------------------------------------------
# Связи между ролями: учитель добавил контрольную → родитель получил уведомление
# ---------------------------------------------------------------------------


def test_teacher_deadline_pushes_notification_to_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = User(
        max_user_id="42", role="teacher", status="approved", school="Школа №1"
    )
    teacher.id = 7
    parent = SimpleNamespace(id=9, max_user_id="700")
    due = datetime(2026, 11, 5, 10, 0)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, teacher)
    main._states["42"] = {
        "step": "dl_kind",
        "class_name": "9А",
        "title": "Алгебра — контрольная",
        "due": due,
    }

    async def create_deadline(
        owner_id: int,
        creator_id: int,
        title: str,
        due_date: datetime,
        kind: str,
        class_name: str | None = None,
    ) -> object:
        return SimpleNamespace(title=title, kind=kind, date=due_date)

    async def parents_of_class_students(
        class_name: str | None, school: str | None = None
    ) -> list[object]:
        assert class_name == "9А"
        assert school == "Школа №1"
        return [parent]

    async def set_state(target: User, step: str | None, **data: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "create_deadline", create_deadline)
    monkeypatch.setattr(
        main.mvp, "parents_of_class_students", parents_of_class_students
    )
    monkeypatch.setattr(main, "_set_state", set_state)

    try:
        asyncio.run(
            main._handle_callback(callback_event("teacher:dl_kind:контрольная", bot))
        )
    finally:
        main._states.pop("42", None)

    push = [entry for entry in sent if entry[3] == 700]
    assert len(push) == 1
    assert push[0][0] is None
    assert "🔔 В классе 9А добавлено событие" in push[0][1]
    assert "Алгебра — контрольная" in push[0][1]
    assert "05.11.2026 10:00" in push[0][1]


# ---------------------------------------------------------------------------
# Кабинет родителя: ближайшие учебные события
# ---------------------------------------------------------------------------


def test_parent_deadlines_button_shows_upcoming_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="parent", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    due = datetime.now() + timedelta(days=2)
    items = [
        SimpleNamespace(id=1, title="Контрольная по физике", kind="контрольная", date=due)
    ]

    async def deadlines_for_parent(target: User) -> list[object]:
        return items

    monkeypatch.setattr(main.mvp, "deadlines_for_parent", deadlines_for_parent)

    asyncio.run(main._handle_callback(callback_event("parent:deadlines", bot)))

    text = sent[0][1]
    assert "Контрольная по физике" in text
    assert due.strftime("%d.%m.%Y") in text
    assert "menu:parent" in payloads_of(sent[0][2])



# ---------------------------------------------------------------------------
# Кабинет психолога: отметка «в работе» / «закрыт» и связь с учеником
# ---------------------------------------------------------------------------


def test_psychologist_signals_render_status_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="psychologist", status="approved")
    user.id = 3
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    signals = [
        SimpleNamespace(
            id=11,
            student=SimpleNamespace(name="Иван", class_name="9А"),
            risk_level=4,
            themes="тревога, усталость",
            status="pending",
            preferred_format="chat",
        )
    ]

    async def psychologist_signals(psychologist_id: int) -> list[object]:
        assert psychologist_id == 3
        return signals

    monkeypatch.setattr(main.mvp, "psychologist_signals", psychologist_signals)

    asyncio.run(main._handle_callback(callback_event("psychologist:signals", bot)))

    text = sent[0][1]
    assert "Сигнал #11" in text
    assert "Иван" in text
    assert "9А" in text
    assert "Риск: 4/5" in text
    assert "Формат: chat" in text
    payloads = payloads_of(sent[0][2])
    assert "psychologist:signal:11:in_progress" in payloads
    assert "psychologist:signal:11:closed" in payloads
    assert "psychologist:contact:11" in payloads


def test_psychologist_contact_messages_student_and_marks_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="psychologist", status="approved")
    user.id = 3
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    statuses: list[tuple[int, str]] = []
    signal = SimpleNamespace(
        id=11,
        student=SimpleNamespace(max_user_id="555", name="Иван"),
        preferred_format="live",
    )

    async def psychologist_signal(psychologist_id: int, signal_id: int) -> object:
        assert (psychologist_id, signal_id) == (3, 11)
        return signal

    async def update_signal_status(signal_id: int, status: str) -> bool:
        statuses.append((signal_id, status))
        return True

    monkeypatch.setattr(main.mvp, "psychologist_signal", psychologist_signal)
    monkeypatch.setattr(main.mvp, "update_signal_status", update_signal_status)

    asyncio.run(main._handle_callback(callback_event("psychologist:contact:11", bot)))

    assert statuses == [(11, "in_progress")]
    direct = [entry for entry in sent if entry[3] == 555]
    assert len(direct) == 1
    assert "школьный психолог" in direct[0][1]
    assert "живая встреча" in direct[0][1]
    assert "останется между нами" in direct[0][1]
    assert "Сигнал #11 отмечен как «в работе»" in sent[-1][1]


def test_psychologist_contact_unknown_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(max_user_id="42", role="psychologist", status="approved")
    user.id = 3
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def psychologist_signal(psychologist_id: int, signal_id: int) -> object:
        return None

    monkeypatch.setattr(main.mvp, "psychologist_signal", psychologist_signal)

    asyncio.run(main._handle_callback(callback_event("psychologist:contact:99", bot)))

    assert "Сигнал не найден." in sent[0][1]

