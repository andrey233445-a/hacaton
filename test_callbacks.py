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
from app.keyboards.inline import ButtonsPayload, menu_keyboard


def callback_event(
    payload: str, bot: Bot
) -> MessageCallback:
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


@pytest.mark.parametrize(
    ("role", "expected_text"),
    [
        ("student", "В каком ты классе?"),
        ("teacher", "В какой школе работаешь?"),
        ("psychologist", "В какой школе работаешь?"),
        ("parent", "Введи числовой MAX ID ребёнка."),
    ],
)
def test_role_selection_callback_reaches_onboarding(
    monkeypatch: pytest.MonkeyPatch, role: str, expected_text: str
) -> None:
    user = User(max_user_id="42", name="Иванов Иван Иванович", role="unselected")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    acknowledgements: list[bool] = []
    bot = Bot(token="test-token")

    async def ack(callback_id: str, **kwargs: object) -> None:
        assert callback_id == "callback-id"
        assert kwargs == {"notification": "Запрос принят."}
        acknowledgements.append(True)

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def update_profile(max_user_id: str, **values: object) -> User:
        assert max_user_id == "42"
        for key, value in values.items():
            setattr(user, key, value)
        return user

    async def set_state(user: User, step: str | None, **data: object) -> None:
        assert user.max_user_id == "42"
        assert step is not None
        assert not data

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    event = callback_event(f"onboard:role:{role}", bot)

    asyncio.run(main._handle_callback(event))

    assert acknowledgements == [True]
    assert user.role == role
    assert len(sent) == 1
    assert expected_text in sent[0][1]
    if role == "student":
        buttons = sent[0][2]
        assert buttons is not None
        payloads = []
        for row in buttons.buttons:
            for button in row:
                assert isinstance(button, CallbackButton)
                payloads.append(button.payload)
        assert set(payloads) == {
            f"onboard:grade:{grade}" for grade in range(1, 12)
        }


def test_staff_role_requests_full_name_when_it_was_not_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id=42, role="unselected")
    sent: list[str] = []

    async def find_user(max_user_id: str) -> User:
        return user

    async def update_profile(max_user_id: str, **values: object) -> User:
        for key, value in values.items():
            setattr(user, key, value)
        return user

    async def set_state(user: User, step: str | None, **data: object) -> None:
        main._states[str(user.max_user_id)] = {"step": step, **data}

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        assert kwargs == {"notification": "Запрос принят."}

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)
    bot = Bot(token="test-token")
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("onboard:role:teacher", bot)))

    assert sent == ["Укажи ФИО — оно появится в профиле."]
    assert main._get_state("42") == {"step": "onboarding_staff_name"}
    main._states.pop("42", None)


def test_staff_start_requests_missing_full_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сотрудник без ФИО получает запрос ФИО — без всяких заявок и ожиданий."""
    user = User(
        max_user_id=42,
        role="teacher",
        status="approved",
    )
    sent: list[str] = []

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
        auto_menu: bool = True,
    ) -> None:
        sent.append(text)

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    event = SimpleNamespace(
        from_user=SimpleNamespace(user_id=42),
        chat=SimpleNamespace(chat_id=10),
        message=SimpleNamespace(body=SimpleNamespace(text="/start")),
    )

    asyncio.run(main._handle_message_created(event))

    assert sent == [
        "В профиле сотрудника не сохранено ФИО. Пожалуйста, введите ФИО."
    ]
    assert main._get_state("42") == {"step": "onboarding_staff_name"}
    main._states.pop("42", None)


def test_staff_school_confirmation_opens_cabinet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После указания школы сотрудник сразу попадает в кабинет — без ожидания."""
    user = User(max_user_id=42, role="teacher")
    sent: list[str] = []

    async def update_profile(max_user_id: str, **values: object) -> User:
        assert max_user_id == "42"
        assert values == {"school": "Гимназия"}
        return user

    async def set_state(user: User, step: str | None, **data: object) -> None:
        assert step is None

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
        auto_menu: bool = True,
    ) -> None:
        sent.append(text)

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="onboarding_school",
            text="Гимназия",
            state={},
        )
    )

    assert handled is True
    assert user.school_name == "Гимназия"
    assert sent[0] == (
        "Школа сохранена. Добро пожаловать в личный кабинет сотрудника!"
    )
    # Следом сразу показываем меню кабинета учителя.
    assert sent[1] == "Кабинет учителя. Выберите действие:"
    assert "заявк" not in " ".join(sent).lower()
    assert "администратор" not in " ".join(sent).lower()


@pytest.mark.parametrize("role", ["student", "teacher", "parent", "psychologist"])
def test_all_role_menus_serialize_callback_buttons(role: str) -> None:
    attachment = menu_keyboard(role).pack().model_dump(mode="json")

    assert attachment["type"] == "inline_keyboard"
    buttons = attachment["payload"]["buttons"]
    assert buttons
    assert all(
        button["type"] == "callback" and button["payload"]
        for row in buttons
        for button in row
    )


def test_teacher_class_registration_step_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher")
    saved: dict[str, object] = {}
    menu_calls: list[tuple[int | None, User, int | None]] = []

    async def update_profile(max_user_id: str, **values: object) -> User:
        assert max_user_id == "42"
        saved.update(values)
        return user

    async def set_state(user: User, step: str | None, **data: object) -> None:
        assert user.role == "teacher"
        assert step is None
        assert not data

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def show_menu(
        chat_id: int | None, user: User, max_id: int | None
    ) -> None:
        menu_calls.append((chat_id, user, max_id))

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_menu", show_menu)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="onboarding_classes",
            text="8А, 9Б",
            state={},
        )
    )

    assert handled is True
    assert saved["classes"] == "8А,9Б"
    assert len(menu_calls) == 1


def test_parent_notifications_button_opens_notification_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="parent", reminders_enabled=True)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    async def ack(callback_id: str, **kwargs: object) -> None:
        assert callback_id == "callback-id"
        assert kwargs == {"notification": "Запрос принят."}
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    event = callback_event("parent:notifications", bot)

    asyncio.run(main._handle_callback(event))

    assert "🔔 Уведомления включены." in sent[0][1]
    buttons = sent[0][2]
    assert buttons is not None
    button = buttons.buttons[0][0]
    assert isinstance(button, CallbackButton)


def test_psychologist_stats_button_opens_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="psychologist", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def signals_summary(psychologist_id: int) -> dict[str, int]:
        assert psychologist_id == user.id
        return {
            "total": 5,
            "pending": 2,
            "in_progress": 2,
            "closed": 1,
        }

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    async def ack(callback_id: str, **kwargs: object) -> None:
        assert callback_id == "callback-id"
        assert kwargs == {"notification": "Запрос принят."}
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "psychologist_signals_summary", signals_summary)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    event = callback_event("psychologist:stats", bot)

    asyncio.run(main._handle_callback(event))

    assert "Статистика сигналов" in sent[0][1]
    assert "Всего: 5" in sent[0][1]


def test_teacher_homework_button_asks_for_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    states: list[tuple[str | None, dict[str, object]]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def classes_for_teacher(teacher_id: int) -> list[object]:
        return [SimpleNamespace(name="9А"), SimpleNamespace(name="11Б")]

    async def set_state(user: User, step: str | None, **data: object) -> None:
        states.append((step, data))

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
    monkeypatch.setattr(main.crud, "classes_for_teacher", classes_for_teacher)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("teacher:homework", bot)))

    assert "Домашка. Выберите класс" in sent[0][1]
    buttons = sent[0][2]
    assert buttons is not None
    payloads = [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]
    assert payloads == ["teacher:homework:class:9А", "teacher:homework:class:11Б"]
    assert states == []


def test_teacher_homework_chain_creates_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher", status="approved")
    user.id = 7
    created: list[tuple[object, ...]] = []
    sent: list[str] = []

    async def create_homework(
        owner_id: int,
        creator_id: int,
        subject: str,
        description: str,
        deadline: datetime,
        class_name: str | None = None,
        points: int = 10,
    ) -> object:
        created.append(
            (owner_id, creator_id, subject, description, deadline, class_name, points)
        )
        return SimpleNamespace(subject=subject, description=description, points=points)

    async def set_state(user: User, step: str | None, **data: object) -> None:
        return None

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    monkeypatch.setattr(main.mvp, "create_homework", create_homework)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="hw_description",
            text="§12, задачи 5-10",
            state={"class_name": "9А", "subject": "Алгебра"},
        )
    )

    assert handled is True
    assert created == []
    assert "Когда сдать?" in sent[-1][0]

    # После срока учитель задаёт стоимость домашки в баллах.
    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="hw_deadline",
            text="25.10.2026 23:59",
            state={
                "class_name": "9А",
                "subject": "Алгебра",
                "description": "§12, задачи 5-10",
            },
        )
    )

    assert handled is True
    assert created == []
    assert "Сколько баллов" in sent[-1][0]
    points_payloads = [
        button.payload
        for row in (sent[-1][1].buttons or [])
        for button in row
    ]
    assert "teacher:hw_points:15" in points_payloads

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="hw_points",
            text="25",
            state={
                "class_name": "9А",
                "subject": "Алгебра",
                "description": "§12, задачи 5-10",
                "due": datetime(2026, 10, 25, 23, 59),
            },
        )
    )

    assert handled is True
    assert len(created) == 1
    (
        owner_id,
        creator_id,
        subject,
        description,
        deadline,
        class_name,
        points,
    ) = created[0]
    assert (owner_id, creator_id) == (7, 7)
    assert subject == "Алгебра"
    assert description == "§12, задачи 5-10"
    assert class_name == "9А"
    assert points == 25
    assert (deadline.year, deadline.month, deadline.day) == (2026, 10, 25)
    assert "Домашка для 9А добавлена" in sent[-1][0]
    assert "Баллы за сдачу: 25" in sent[-1][0]


def test_teacher_points_button_creates_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Баллы можно выбрать кнопкой — без ручного ввода числа."""
    user = User(max_user_id="42", role="teacher", status="approved")
    user.id = 7
    created: list[tuple[object, ...]] = []
    sent: list[str] = []
    bot = Bot(token="test-token")
    main._states["42"] = {
        "step": "hw_points",
        "class_name": "9А",
        "subject": "Алгебра",
        "description": "§12",
        "due": datetime(2026, 10, 25, 23, 59),
    }

    async def create_homework(
        owner_id: int,
        creator_id: int,
        subject: str,
        description: str,
        deadline: datetime,
        class_name: str | None = None,
        points: int = 10,
    ) -> object:
        created.append((subject, class_name, points))
        return SimpleNamespace(subject=subject, description=description, points=points)

    async def find_user(max_user_id: str) -> User:
        return user

    async def set_state(user: User, step: str | None, **data: object) -> None:
        return None

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "create_homework", create_homework)
    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("teacher:hw_points:20", bot)))

    assert created == [("Алгебра", "9А", 20)]
    assert "Баллы за сдачу: 20" in sent[0]


def test_teacher_schedule_day_button_advances_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher", status="approved")
    states: list[tuple[str | None, dict[str, object]]] = []
    sent: list[str] = []
    bot = Bot(token="test-token")
    main._states["42"] = {"step": "sch_day", "class_name": "9А"}

    async def find_user(max_user_id: str) -> User:
        return user

    async def set_state(user: User, step: str | None, **data: object) -> None:
        states.append((step, data))

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    try:
        monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
        monkeypatch.setattr(main, "_set_state", set_state)
        monkeypatch.setattr(main, "_send", send)
        monkeypatch.setattr(bot, "send_callback", ack)

        asyncio.run(
            main._handle_callback(callback_event("teacher:schedule_day:3", bot))
        )

        assert states == [("sch_time", {"class_name": "9А", "day": 3})]
        assert "Среда" in sent[0]
        assert "ЧЧ:ММ" in sent[0]
    finally:
        main._states.pop("42", None)


def test_student_schedule_button_shows_lessons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved", class_name="9А")
    user.id = 5
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    lessons = [
        SimpleNamespace(
            id=1,
            user_id=5,
            created_by=5,
            time=time(hour=8, minute=30),
            subject="Алгебра",
            room="204",
        ),
        SimpleNamespace(
            id=2,
            user_id=5,
            created_by=5,
            time=time(hour=9, minute=30),
            subject="Физика",
            room=None,
        ),
    ]
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def schedule_for_day(target: User, weekday: int) -> list[object]:
        assert target is user
        assert weekday == datetime.now().isoweekday()
        return lessons

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
    monkeypatch.setattr(main.mvp, "schedule_for_day", schedule_for_day)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:schedule", bot)))

    text = sent[0][1]
    assert "08:30 — Алгебра, каб. 204" in text
    assert "09:30 — Физика" in text
    buttons = sent[0][2]
    assert buttons is not None
    payloads = [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]
    assert "student:schedule:1" in payloads
    assert "student:schedule:7" in payloads
    # расписание на сегодня/завтра/неделю; уроки ведёт учитель —
    # ученик не может добавлять или удалять их.
    assert "student:schedule_week" in payloads
    assert "student:schedule_add" not in payloads
    assert not any(
        payload.startswith("student:lesson_del:") for payload in payloads
    )


def test_student_homework_done_is_teacher_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Готовность домашки отмечает только учительница — ученику отказ."""
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def complete_homework(target: User, homework_id: int) -> bool:
        raise AssertionError("Ученик не должен иметь возможности отметить сдачу")

    async def active_homework(target: User) -> list[tuple[object, bool]]:
        return []

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "complete_homework", complete_homework)
    monkeypatch.setattr(main.mvp, "active_homework", active_homework)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(
        main._handle_callback(callback_event("student:homework_done:5", bot))
    )

    assert any("учитель" in text for text in sent)
    assert "активных заданий нет" in sent[-1]


def test_teacher_hw_toggle_marks_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = User(max_user_id="7", role="teacher", status="approved")
    student = SimpleNamespace(id=9, name="Иванов Иван")
    calls: list[tuple[int, int, bool]] = []
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return teacher

    async def roster(target: User, homework_id: int) -> list[tuple[object, bool]]:
        assert target is teacher and homework_id == 5
        return [(student, False)]

    async def set_done(target: User, homework_id: int, student_id: int, done: bool) -> bool:
        calls.append((homework_id, student_id, done))
        return True

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "homework_roster", roster)
    monkeypatch.setattr(main.mvp, "set_homework_completion", set_done)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(
        main._handle_callback(callback_event("teacher:hw_toggle:5:9", bot))
    )

    assert calls == [(5, 9, True)]
    assert "Кто сдал" in sent[-1]


def test_student_deadlines_button_shows_countdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    due = datetime.now() + timedelta(days=2, hours=2)
    items = [
        SimpleNamespace(title="Алгебра — контрольная", date=due, kind="контрольная")
    ]
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def upcoming_deadlines(target: User) -> list[object]:
        assert target is user
        return items

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
    monkeypatch.setattr(main.mvp, "upcoming_deadlines", upcoming_deadlines)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:deadlines", bot)))

    text = sent[0][1]
    assert "Алгебра — контрольная" in text
    assert "осталось 2 дня" in text
    buttons = sent[0][2]
    assert buttons is not None
    payloads = [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]
    # дедлайны задаёт учитель — кнопки добавления у ученика нет.
    assert "student:deadline_add" not in payloads


def test_student_stress_button_opens_antistress_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
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

    asyncio.run(main._handle_callback(callback_event("student:stress", bot)))

    assert "Антистресс" in sent[0][1]
    buttons = sent[0][2]
    assert buttons is not None
    payloads = [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]
    assert "student:mood_ask" in payloads
    assert "student:breathing" in payloads
    assert "student:mood_history" in payloads


def test_student_mood_button_saves_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    scores: list[int] = []
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def save_mood(target: User, score: int, comment: str | None = None) -> bool:
        assert target is user
        scores.append(score)
        return True

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "save_mood", save_mood)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:mood:4", bot)))

    assert scores == [4]
    assert "+3 очка" in sent[0]


def test_student_low_mood_offers_psychologist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[str, ButtonsPayload | None]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def save_mood(target: User, score: int, comment: str | None = None) -> bool:
        return True

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "save_mood", save_mood)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:mood:2", bot)))

    assert "непростым" in sent[0][0]
    buttons = sent[0][1]
    assert buttons is not None
    payloads = [
        button.payload for row in buttons.buttons for button in row
    ]
    assert "student:ai_chat" in payloads
    assert "stress:yes" in payloads


def test_teacher_announcement_pushed_to_class_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = User(max_user_id="42", role="teacher", status="approved")
    student = User(max_user_id="77", role="student", status="approved")
    sent: list[tuple[int | None, str, int | None]] = []

    async def create_announcement(
        teacher_id: int, class_name: str, message: str
    ) -> None:
        return None

    async def recipients(teacher_id: int, class_name: str) -> list[User]:
        return [student]

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, user_id))

    monkeypatch.setattr(main.mvp, "create_announcement", create_announcement)
    monkeypatch.setattr(main.mvp, "announcement_recipients", recipients)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=teacher,
            step="announce_text",
            text="Завтра едем в музей",
            state={"class_name": "11А"},
        )
    )

    assert handled is True
    assert sent[0] == (None, "📢 Объявление класса 11А:\nЗавтра едем в музей", 77)
    assert "доставлено учеников: 1" in sent[1][1]


def test_teacher_stats_button_shows_completion_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher", status="approved")
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def today_counts(teacher: User) -> tuple[int, int]:
        assert teacher is user
        return 4, 7

    async def completion_report(teacher: User) -> list[tuple[str, int, int]]:
        return [("Иванов Иван", 3, 4), ("Петрова Анна", 4, 4)]

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "today_counts_for_teacher", today_counts)
    monkeypatch.setattr(main.mvp, "teacher_completion_report", completion_report)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("teacher:stats", bot)))

    text = sent[0]
    assert "Активных заданий: 4" in text
    assert "Отметок о выполнении: 7" in text
    assert "Иванов Иван: 3/4" in text
    assert "Петрова Анна: 4/4" in text


def test_deadline_kind_button_creates_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher", status="approved")
    user.id = 7
    due = datetime(2026, 11, 5, 10, 0)
    created: list[tuple[object, ...]] = []
    notified_classes: list[str] = []
    sent: list[str] = []
    bot = Bot(token="test-token")
    main._states["42"] = {
        "step": "dl_kind",
        "class_name": "9А",
        "title": "Алгебра — контрольная",
        "due": due,
    }

    async def find_user(max_user_id: str) -> User:
        return user

    async def create_deadline(
        owner_id: int,
        creator_id: int,
        title: str,
        due_date: datetime,
        kind: str,
        class_name: str | None = None,
    ) -> object:
        created.append((owner_id, title, due_date, kind, class_name))
        return SimpleNamespace(title=title, kind=kind, date=due_date)

    async def parents_of_class_students(
        class_name: str | None, school: str | None = None
    ) -> list[User]:
        notified_classes.append(class_name or "")
        return []

    async def set_state(user: User, step: str | None, **data: object) -> None:
        return None

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    try:

        monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
        monkeypatch.setattr(main.mvp, "create_deadline", create_deadline)
        monkeypatch.setattr(
            main.mvp, "parents_of_class_students", parents_of_class_students
        )
        monkeypatch.setattr(main, "_set_state", set_state)
        monkeypatch.setattr(main, "_send", send)
        monkeypatch.setattr(bot, "send_callback", ack)

        asyncio.run(
            main._handle_callback(
                callback_event("teacher:dl_kind:контрольная", bot)
            )
        )

        assert created == [(7, "Алгебра — контрольная", due, "контрольная", "9А")]
        assert "Дедлайн для 9А добавлен" in sent[0]
        assert "05.11.2026 10:00" in sent[0]
        # учитель добавил контрольную → ищем родителей для уведомления.
        assert notified_classes == ["9А"]
    finally:
        main._states.pop("42", None)


def test_student_homework_add_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """домашку задаёт учитель по своему предмету."""
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:homework_add", bot)))

    assert "задаёт учитель" in sent[0]


def test_student_deadline_add_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """дедлайны задаёт учитель — ученик только смотрит."""
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:deadline_add", bot)))

    assert "задаёт учитель" in sent[0]


def test_student_dl_kind_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ученик не может завершить создание дедлайна выбором типа."""
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[str] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def create_deadline(*args: object, **kwargs: object) -> object:
        raise AssertionError("ученик не должен создавать дедлайн")

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "create_deadline", create_deadline)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(
        main._handle_callback(callback_event("student:dl_kind:контрольная", bot))
    )

    assert "создаёт учитель" in sent[0]


def test_student_hw_fsm_step_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FSM-шаг добавления домашки блокируется для ученика."""
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[str] = []
    steps: list[str | None] = []

    async def set_state(target: User, step: str | None, **data: object) -> None:
        steps.append(step)

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(10, 42, user, "hw_subject", "Алгебра", {})
    )

    assert handled is True
    assert steps == [None]
    assert "задаёт учитель" in sent[0]


def test_reminder_settings_panel_and_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настройки: панель сроков напоминаний и переключатель «за 3 дня»."""
    user = User(max_user_id="42", role="student", status="approved")
    user.id = 1
    user.notify_3d = True
    updates: list[dict[str, object]] = []
    sent: list[tuple[str, ButtonsPayload | None]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
        return user

    async def update_profile(max_user_id: object, **values: object) -> User:
        updates.append(values)
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("settings:reminders", bot)))

    assert "Настройки напоминаний" in sent[0][0]
    buttons = sent[0][1]
    assert buttons is not None
    payloads = [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]
    assert {
        "settings:reminders:all",
        "settings:reminders:7d",
        "settings:reminders:3d",
        "settings:reminders:1d",
    } <= set(payloads)

    asyncio.run(main._handle_callback(callback_event("settings:reminders:3d", bot)))

    assert updates == [{"notify_3d": False}]
    assert "Настройки напоминаний" in sent[-1][0]


def _dev_message_event(text: str, user_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(user_id=user_id),
        chat=SimpleNamespace(chat_id=10),
        message=SimpleNamespace(body=SimpleNamespace(text=text)),
    )


def test_dev_command_opens_hidden_panel_for_team_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id=42, role="student", status="approved", class_name="10А")
    sent: list[tuple[str, ButtonsPayload | None]] = []

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "42"
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    monkeypatch.setattr(main.config, "DEV_COMMAND", "/dev")
    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)

    asyncio.run(main._handle_message_created(_dev_message_event("/dev")))

    assert len(sent) == 1
    text, buttons = sent[0]
    assert "Панель разработчика" in text
    assert "🎓 Ученик" in text
    payloads = [b.payload for row in buttons.buttons for b in row]
    assert "dev:role:teacher" in payloads
    # Одобрение заявок убрано: администратора в продукте нет.
    assert "dev:approve" not in payloads
    assert "dev:reset" in payloads
    assert "dev:menu" in payloads
    assert "dev:directory" in payloads
    assert "dev:test_signal" in payloads


def test_dev_command_is_invisible_for_regular_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id=7, role="teacher", status="approved")
    sent: list[tuple[str, ButtonsPayload | None]] = []

    async def find_user(max_user_id: str) -> User:
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    monkeypatch.setattr(main.config, "DEV_COMMAND", "/dev")
    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)

    asyncio.run(main._handle_message_created(_dev_message_event("/dev", user_id=7)))

    # Ответ неотличим от реакции на любой неизвестный текст — никаких следов панели.
    assert len(sent) == 1
    assert sent[0][0] == "Используйте кнопки меню."
    # Но пользователь не застревает: кнопка возврата в кабинет на месте.
    assert sent[0][1] is not None
    assert "menu:teacher" in [
        button.payload for row in sent[0][1].buttons for button in row
    ]


def test_dev_role_switch_works_at_any_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id=42, role="student", status="approved")
    sent: list[str] = []
    forced: list[dict[str, object]] = []

    async def find_user(max_user_id: str) -> User:
        return user

    async def force_profile(max_user_id: str, **values: object) -> User:
        forced.append(dict(values))
        for key, value in values.items():
            setattr(user, key, value)
        return user

    async def set_state(u: User, step: str | None, **data: object) -> None:
        assert step is None

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "dev_force_profile", force_profile)
    monkeypatch.setattr(main, "_set_state", set_state)
    monkeypatch.setattr(main, "_send", send)
    bot = Bot(token="test-token")
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("dev:role:psychologist", bot)))

    assert forced == [{"role": "psychologist", "status": "approved"}]
    assert user.role == "psychologist"
    assert "Панель разработчика" in sent[-1]
    assert "🧠 Психолог" in sent[-1]


def test_dev_callbacks_are_silently_ignored_for_regular_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id=7, role="student", status="approved")
    sent: list[str] = []
    forced: list[dict[str, object]] = []

    async def find_user(max_user_id: str) -> User:
        return user

    async def force_profile(max_user_id: str, **values: object) -> User:
        forced.append(dict(values))
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def ack(callback_id: str, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main.mvp, "dev_force_profile", force_profile)
    monkeypatch.setattr(main, "_send", send)
    bot = Bot(token="test-token")
    monkeypatch.setattr(bot, "send_callback", ack)

    event = callback_event("dev:role:teacher", bot)
    event.callback.user = MaxUser(user_id=7, first_name="Test", is_bot=False)

    asyncio.run(main._handle_callback(event))

    assert forced == []
    assert sent == []
    assert user.role == "student"
