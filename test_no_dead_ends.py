"""Проверки сценариев «без тупиков» (замечания команды).

Покрывают три области:
- доступ к живому школьному психологу (школа находится даже при разном написании,
  есть выбор школы кнопками и дежурный психолог);
- возврат в меню из любого экрана, включая напоминания и отказы в доступе;
- баллы за домашку и колесо удачи за очки.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from maxapi import Bot
from maxapi.enums.chat_type import ChatType
from maxapi.types import CallbackButton, Message, MessageCallback, Recipient
from maxapi.types.callback import Callback
from maxapi.types.users import User as MaxUser

from app import main
from app.db.models import User
from app.keyboards.inline import ButtonsPayload


@pytest.fixture(autouse=True)
def clean_states():
    """FSM-состояния и dev-входы «как» живут в памяти — чистим их между тестами."""
    main._states.clear()
    main._impersonation.clear()
    yield
    main._states.clear()
    main._impersonation.clear()


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
    user: User | None,
) -> Bot:
    """Бот с перехватом _send, ack и поиска пользователя."""
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str):
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
        auto_menu: bool = True,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)
    return bot


def payloads_of(buttons: ButtonsPayload | None) -> list[str]:
    if buttons is None:
        return []
    return [
        button.payload
        for row in buttons.buttons
        for button in row
        if isinstance(button, CallbackButton)
    ]


def student(**overrides) -> User:
    base: dict = {
        "max_user_id": "42",
        "role": "student",
        "status": "approved",
        "name": "Иванов Иван",
        "class_name": "9А",
    }
    base.update(overrides)
    user = User(**base)
    user.id = 7
    return user


def psychologist(**overrides) -> User:
    base: dict = {
        "max_user_id": "99",
        "role": "psychologist",
        "status": "approved",
        "name": "Петрова Анна",
        "school_name": "Школа №1",
    }
    base.update(overrides)
    user = User(**base)
    user.id = 3
    return user


def signal_stubs(
    monkeypatch: pytest.MonkeyPatch,
    psy: User | None,
    signals: list[dict],
    schools: list[str] | None = None,
) -> None:
    """Обвязка mvp для сценариев передачи сигнала психологу."""

    async def for_school(school_name: str):
        return psy

    async def school_list() -> list[str]:
        return schools if schools is not None else (
            [psy.school_name] if psy is not None else []
        )

    async def any_psy():
        return psy

    async def by_id(user_id: int):
        return psy

    async def last_message(target: User):
        return None

    async def open_signal(student_id: int, psychologist_id: int) -> bool:
        return False

    async def snapshot(target: User):
        # Пустая история переписки → сигнал уходит с риском «4» и без тем.
        return 4, []

    async def create_signal(student_id, psychologist_id, **kwargs):
        signals.append({"psychologist_id": psychologist_id, **kwargs})
        return SimpleNamespace(
            id=11, risk_level=kwargs["risk_level"], themes=kwargs["themes"]
        )

    monkeypatch.setattr(main.mvp, "psychologist_for_school", for_school)
    monkeypatch.setattr(main.mvp, "psychologist_schools", school_list)
    monkeypatch.setattr(main.mvp, "any_psychologist", any_psy)
    monkeypatch.setattr(main.mvp, "user_by_id", by_id)
    monkeypatch.setattr(main.mvp, "last_chat_message", last_message)
    monkeypatch.setattr(main.mvp, "has_open_signal", open_signal)
    monkeypatch.setattr(main.mvp, "stress_snapshot", snapshot)
    monkeypatch.setattr(main.mvp, "create_stress_signal", create_signal)



# --------------------------------------------------------------------------- #
# Доступ к живому психологу
# --------------------------------------------------------------------------- #
def test_signal_offers_school_picker_when_school_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без школы ученик получает список школ с психологами, а не тупик."""
    user = student(school_name=None)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    signals: list[dict] = []
    signal_stubs(monkeypatch, None, signals, schools=["Школа №1", "Гимназия №5"])

    asyncio.run(main._handle_callback(callback_event("stress:live", bot)))

    payloads = payloads_of(sent[0][2])
    assert "stress:school:0" in payloads
    assert "stress:school:1" in payloads
    assert "stress:school_manual" in payloads
    assert "stress:any" in payloads
    assert "stress:help" in payloads
    assert "menu:student" in payloads
    # Список школ сохранён в состоянии, чтобы кнопка-индекс работала.
    assert main._states["42"]["schools"] == ["Школа №1", "Гимназия №5"]
    assert signals == []


def test_signal_created_when_school_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = student(school_name="школа n1")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    signals: list[dict] = []
    signal_stubs(monkeypatch, psychologist(), signals)

    asyncio.run(main._handle_callback(callback_event("stress:live", bot)))

    assert signals == [
        {
            "psychologist_id": 3,
            "risk_level": 4,
            "themes": [],
            "preferred_format": "live",
            "consent": True,
        }
    ]
    # Ученику — подтверждение и экстренные контакты, психологу — уведомление.
    assert "Сигнал передан психологу" in sent[0][1]
    assert "8-800-2000-122" in sent[0][1]
    assert "menu:student" in payloads_of(sent[0][2])
    assert sent[1][3] == 99
    assert "Новый сигнал" in sent[1][1]
    assert "psychologist:signals" in payloads_of(sent[1][2])


def test_school_button_saves_school_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Выбор школы кнопкой сохраняет профиль и сразу создаёт сигнал."""
    user = student(school_name=None)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    saved: list[dict] = []
    signals: list[dict] = []
    signal_stubs(
        monkeypatch,
        psychologist(school_name="Гимназия №5"),
        signals,
        schools=["Школа №1", "Гимназия №5"],
    )
    main._states["42"] = {
        "step": "stress_school",
        "stress_format": "chat",
        "schools": ["Школа №1", "Гимназия №5"],
    }

    async def update_profile(max_user_id: str, **values):
        saved.append(values)
        return user

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)

    asyncio.run(main._handle_callback(callback_event("stress:school:1", bot)))

    assert saved == [{"school": "Гимназия №5"}]
    assert signals == [
        {
            "psychologist_id": 3,
            "risk_level": 4,
            "themes": [],
            "preferred_format": "chat",
            "consent": True,
        }
    ]
    assert user.school_name == "Гимназия №5"


def test_duty_psychologist_requires_consent_and_creates_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если психолога школы нет — сигнал уходит дежурному, но только с согласия."""
    user = student(school_name="Школа №99")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    signals: list[dict] = []
    signal_stubs(monkeypatch, psychologist(), signals, schools=["Школа №1"])

    async def for_school(school_name: str):
        return None

    monkeypatch.setattr(main.mvp, "psychologist_for_school", for_school)

    # Шаг 1: психолог школы не найден — показываем список и дежурного.
    asyncio.run(main._handle_callback(callback_event("stress:chat", bot)))
    assert "психолог пока не зарегистрирован" in sent[0][1]
    assert "stress:any" in payloads_of(sent[0][2])

    # Шаг 2: спрашиваем согласие на дежурного психолога.
    asyncio.run(main._handle_callback(callback_event("stress:any", bot)))
    assert "дежурному психологу" in sent[1][1].lower()
    assert "stress:any_confirm" in payloads_of(sent[1][2])
    assert signals == []

    # Шаг 3: после согласия сигнал создан.
    asyncio.run(main._handle_callback(callback_event("stress:any_confirm", bot)))
    assert signals == [
        {
            "psychologist_id": 3,
            "risk_level": 4,
            "themes": [],
            "preferred_format": "chat",
            "consent": True,
        }
    ]


def test_no_psychologists_still_gives_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Даже без психологов в системе ученик получает ИИ-поддержку и телефоны."""
    user = student(school_name="Школа №99")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    signals: list[dict] = []

    async def for_school(school_name: str):
        return None

    signal_stubs(monkeypatch, None, signals, schools=[])
    monkeypatch.setattr(main.mvp, "psychologist_for_school", for_school)

    asyncio.run(main._handle_callback(callback_event("stress:chat", bot)))

    assert "8-800-2000-122" in sent[0][1]
    assert "student:ai_chat" in payloads_of(sent[0][2])
    assert "menu:student" in payloads_of(sent[0][2])
    assert signals == []
    # Администратора в продукте нет — и в текстах бота тоже.
    assert "администратор" not in sent[0][1].lower()
    assert "зарегистрироваться" in sent[0][1]


def test_emergency_button_shows_hotlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = student()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("stress:help", bot)))

    assert "103" in sent[0][1]
    assert "8-800-2000-122" in sent[0][1]
    assert "student:talk_psychologist" in payloads_of(sent[0][2])


def test_manual_school_entry_continues_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ручной ввод школы тоже продолжает сценарий, а не обрывается."""
    user = student(school_name=None)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    make_bot(monkeypatch, sent, user)
    signals: list[dict] = []
    saved: list[dict] = []
    signal_stubs(monkeypatch, psychologist(), signals, schools=["Школа №1"])

    async def update_profile(max_user_id: str, **values):
        saved.append(values)
        return user

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="stress_school",
            text="Школа №1",
            state={"stress_format": "live", "schools": ["Школа №1"]},
        )
    )

    assert handled is True
    assert saved == [{"school": "Школа №1"}]
    assert signals[0]["preferred_format"] == "live"


# --------------------------------------------------------------------------- #
# Возврат в меню: ни один экран не должен требовать /start
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("legacy_status", ["pending", "rejected"])
def test_legacy_status_is_activated_without_admin(
    monkeypatch: pytest.MonkeyPatch, legacy_status: str
) -> None:
    """Старые статусы «ожидает»/«отклонён» активируются сами: администратора нет."""
    user = User(
        max_user_id="42", role="teacher", status=legacy_status, name="Марь Иванна"
    )
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    activated: list[str] = []

    async def approve_user(max_user_id: str):
        activated.append(max_user_id)
        user.status = "approved"
        return user

    monkeypatch.setattr(main.mvp, "approve_user", approve_user)

    asyncio.run(main._handle_callback(callback_event("menu:teacher", bot)))

    assert activated == ["42"]
    # Кабинет открылся сразу, без заявок и ожиданий.
    assert sent[0][1] == "Кабинет учителя. Выберите действие:"


def test_restart_button_opens_own_cabinet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = student()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    opened: list[User] = []

    async def menu(chat_id, target, max_id=None):
        opened.append(target)

    monkeypatch.setattr(main, "_menu", menu)

    asyncio.run(main._handle_callback(callback_event("onboard:restart", bot)))

    assert opened == [user]


def test_restart_button_shows_role_choice_for_new_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="unselected", status="approved")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    asyncio.run(main._handle_callback(callback_event("onboard:restart", bot)))

    assert "onboard:role:student" in payloads_of(sent[0][2])
    assert "onboard:role:staff" in payloads_of(sent[0][2])
    assert "onboard:role:parent" in payloads_of(sent[0][2])


def test_reminder_message_has_menu_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Напоминание планировщика приходит с кнопкой возврата в кабинет."""
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    user = User(max_user_id="42", role="parent", status="approved")

    async def find_user(max_user_id: str):
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)

    asyncio.run(main._send_reminder(42, "🔔 Напоминание о контрольной"))

    assert sent[0][3] == 42
    assert "menu:parent" in payloads_of(sent[0][2])


def test_set_school_step_offers_psychologist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После сохранения школы ученик может сразу продолжить к психологу."""
    user = student(school_name=None)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    make_bot(monkeypatch, sent, user)
    saved: list[dict] = []

    async def update_profile(max_user_id: str, **values):
        saved.append(values)
        return user

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="set_school",
            text="Школа №1",
            state={},
        )
    )

    assert handled is True
    assert saved == [{"school": "Школа №1"}]
    payloads = payloads_of(sent[0][2])
    assert "student:talk_psychologist" in payloads
    assert "student:settings" in payloads
    assert "menu:student" in payloads


# --------------------------------------------------------------------------- #
# Баллы за домашку и новые награды за очки
# --------------------------------------------------------------------------- #
def test_homework_points_prompt_offers_quick_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Учитель задаёт стоимость домашки: числом или кнопкой."""
    user = User(max_user_id="42", role="teacher", status="approved")
    user.id = 7
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    make_bot(monkeypatch, sent, user)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="hw_deadline",
            text="25.10.2026 23:59",
            state={"class_name": "9А", "subject": "Алгебра", "description": "§12"},
        )
    )

    assert handled is True
    assert "Сколько баллов" in sent[0][1]
    payloads = payloads_of(sent[0][2])
    assert "teacher:hw_points:10" in payloads
    assert "teacher:hw_points:50" in payloads


def test_invalid_points_asked_again_with_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="teacher", status="approved")
    user.id = 7
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    make_bot(monkeypatch, sent, user)
    created: list[int] = []

    async def create_homework(*args, points: int = 10, **kwargs):
        created.append(points)
        return SimpleNamespace(subject="Алгебра", description="§12", points=points)

    monkeypatch.setattr(main.mvp, "create_homework", create_homework)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="hw_points",
            text="много",
            state={
                "class_name": "9А",
                "subject": "Алгебра",
                "description": "§12",
                "due": datetime(2026, 10, 25, 23, 59),
            },
        )
    )

    assert handled is True
    assert created == []
    assert "от 0 до 100" in sent[0][1]
    assert "teacher:hw_points:10" in payloads_of(sent[0][2])


def test_reward_catalog_covers_every_profile_badge() -> None:
    """У каждой награды есть описание, цена и значок для профиля."""
    from app.services import mvp

    # Каталог урезан до крутых наград — остались титулы, оформление и усиления.
    assert len(mvp.REWARD_CATALOG) >= 8
    for key, (title, cost, description) in mvp.REWARD_CATALOG.items():
        assert title.strip()
        assert isinstance(cost, int) and cost > 0
        assert len(description) > 10
        assert key in main.REWARD_PROFILE_EMOJI


def test_new_rewards_are_present() -> None:
    from app.services import mvp

    for key in (
        "points_boost_3",
        "points_boost",
        "daily_spin",
        "card_pack",
        "theme_sunset",
        "title_hero",
        "title_captain",
        "title_genius",
        "emoji_cosmos",
        "frame_gold",
        "title_champion",
    ):
        assert key in mvp.REWARD_CATALOG


def test_spin_requires_reward(monkeypatch: pytest.MonkeyPatch) -> None:
    user = student(points=10)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def reward_keys(user_id: int) -> set[str]:
        return set()

    monkeypatch.setattr(main.mvp, "user_reward_keys", reward_keys)

    asyncio.run(main._handle_callback(callback_event("student:spin", bot)))

    assert "Колесо удачи открывается" in sent[0][1]
    assert "student:rewards" in payloads_of(sent[0][2])


def test_spin_awards_prize_once_per_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = student(points=10)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    prizes = iter([20, None])

    async def reward_keys(user_id: int) -> set[str]:
        return {"daily_spin"}

    async def spin(target: User):
        return next(prizes)

    monkeypatch.setattr(main.mvp, "user_reward_keys", reward_keys)
    monkeypatch.setattr(main.mvp, "daily_spin", spin)

    asyncio.run(main._handle_callback(callback_event("student:spin", bot)))
    assert "Тебе выпало 20 очков" in sent[0][1]

    asyncio.run(main._handle_callback(callback_event("student:spin", bot)))
    assert "уже крутил колесо" in sent[1][1]


def test_rewards_list_is_paginated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Каталог наград большой — показываем его страницами с кнопкой «Ещё»."""
    user = student(points=500)
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)

    async def reward_keys(user_id: int) -> set[str]:
        return {"daily_spin"}

    monkeypatch.setattr(main.mvp, "user_reward_keys", reward_keys)

    asyncio.run(main._handle_callback(callback_event("student:rewards", bot)))

    text = sent[0][1]
    assert "Страница 1 из" in text
    payloads = payloads_of(sent[0][2])
    assert "student:rewards:1" in payloads
    # Колесо удачи открыто — кнопка спина на месте.
    assert "student:spin" in payloads
    assert "menu:student" in payloads
    # Кнопок покупки не больше, чем наград на странице (по две в ряд).
    buy_buttons = [item for item in payloads if item.startswith("student:reward_buy:")]
    assert 0 < len(buy_buttons) <= main.REWARDS_PER_PAGE


def test_mood_points_are_flat() -> None:
    """Отметка настроения даёт фиксированные очки — платных бустов больше нет."""
    from app.services import mvp

    morning = datetime(2026, 10, 20, 9, 0)
    evening = datetime(2026, 10, 20, 20, 0)
    assert mvp._mood_points(set(), evening) == 3
    assert mvp._mood_points({"points_boost_3"}, morning) == 3


def test_points_word_pluralization() -> None:
    assert main._points_word(1) == "очко"
    assert main._points_word(3) == "очка"
    assert main._points_word(6) == "очков"
    assert main._points_word(11) == "очков"
    assert main._points_word(21) == "очко"


# --------------------------------------------------------------------------- #
# Dev-панель: диагностика «почему сигнал не доходит» и демо-сигнал
# --------------------------------------------------------------------------- #
def test_dev_directory_lists_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    user = psychologist()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})

    async def directory(limit: int = 40):
        return [user, student(school_name=None)]

    monkeypatch.setattr(main.mvp, "list_directory", directory)

    asyncio.run(main._handle_callback(callback_event("dev:directory", bot)))

    text = sent[0][1]
    assert "Аккаунты в системе: 2" in text
    assert "Школа №1" in text
    assert "dev:menu_panel" in payloads_of(sent[0][2])


def test_dev_directory_warns_when_no_psychologist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Главная причина «школьник не может написать» — психолога просто нет."""
    user = student()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})

    async def directory(limit: int = 40):
        return [user]

    monkeypatch.setattr(main.mvp, "list_directory", directory)

    asyncio.run(main._handle_callback(callback_event("dev:directory", bot)))

    assert "Психологов нет" in sent[0][1]
    assert "один MAX ID = одна роль" in sent[0][1]


def test_dev_test_signal_creates_demo_student(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Демо-сигнал позволяет показать кабинет психолога с одного аккаунта."""
    user = psychologist()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    created: list[tuple] = []
    demo_calls: list[dict] = []

    async def ensure_demo(school_name=None, class_name=None):
        demo_calls.append({"school_name": school_name, "class_name": class_name})
        demo = student(school_name=school_name, class_name=class_name)
        demo.id = 55
        return demo

    async def create_signal(student_id, psychologist_id, **kwargs):
        created.append((student_id, psychologist_id, kwargs))
        return SimpleNamespace(id=31, risk_level=kwargs["risk_level"], themes=kwargs["themes"])

    monkeypatch.setattr(main.mvp, "ensure_demo_student", ensure_demo)
    monkeypatch.setattr(main.mvp, "create_stress_signal", create_signal)

    asyncio.run(main._handle_callback(callback_event("dev:test_signal", bot)))

    assert demo_calls == [{"school_name": "Школа №1", "class_name": "9А"}]
    assert created == [
        (
            55,
            3,
            {
                "risk_level": 4,
                "themes": ["демо-сигнал", "тревога перед экзаменом"],
                "preferred_format": "chat",
            },
        )
    ]
    assert "Тестовый сигнал №31" in sent[0][1]
    assert "psychologist:signals" in payloads_of(sent[0][2])


def test_dev_test_signal_requires_psychologist_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = student()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})

    asyncio.run(main._handle_callback(callback_event("dev:test_signal", bot)))

    assert "переключите роль" in sent[0][1].lower()
    assert "dev:menu_panel" in payloads_of(sent[0][2])


# --------------------------------------------------------------------------- #
# Регистрация без одобрения: администратора в продукте нет
# --------------------------------------------------------------------------- #
def test_every_role_is_active_immediately() -> None:
    from app.db.models import RequestStatus
    from app.services import user_service

    for role in ("student", "teacher", "parent", "psychologist", "unselected"):
        assert user_service._initial_status(role) == RequestStatus.APPROVED


def test_psychologist_role_selection_activates_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Выбор роли психолога сразу даёт доступ — без заявок и ожидания."""
    user = User(max_user_id="42", role="unselected", name="Петрова Анна")
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    values_seen: list[dict] = []

    async def update_profile(max_user_id: str, **values):
        values_seen.append(values)
        for key, value in values.items():
            setattr(user, key, value)
        return user

    monkeypatch.setattr(main.mvp, "update_profile", update_profile)

    asyncio.run(main._handle_callback(callback_event("onboard:role:psychologist", bot)))

    assert values_seen == [{"role": "psychologist", "status": "approved"}]
    assert "В какой школе работаешь?" in sent[0][1]


def test_bot_texts_do_not_mention_administrator() -> None:
    """В пользовательских строках бота не осталось упоминаний администратора.

    Проверяем только строковые литералы (включая части f-строк) и не трогаем
    докстринги: в них как раз объясняется, почему одобрение убрано.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/main.py").read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))

    forbidden = ("администратор", "заявк", "одобр")
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            lowered = node.value.lower()
            if any(word in lowered for word in forbidden):
                hits.append(node.value.strip()[:70])
    assert hits == []


# --------------------------------------------------------------------------- #
# Dev-режим «войти как тестовый ученик»: весь путь с одного аккаунта
# --------------------------------------------------------------------------- #
def _two_account_bot(
    monkeypatch: pytest.MonkeyPatch,
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]],
    accounts: dict[str, User],
) -> Bot:
    """Бот, у которого каждый MAX ID resolves в свой аккаунт."""
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str):
        return accounts.get(str(max_user_id))

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
        auto_menu: bool = True,
    ) -> None:
        sent.append((chat_id, text, buttons, user_id))

    async def ack(callback_id: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)
    return bot


def test_dev_impersonation_lets_one_account_play_student(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Психолог может пройти путь ученика, не выходя из своего чата."""
    psy = psychologist(max_user_id="42")
    demo = student(
        max_user_id="900001",
        name="Тестовый ученик (демо)",
        school_name="Школа №1",
    )
    demo.id = 55
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = _two_account_bot(
        monkeypatch, sent, {"42": psy, "900001": demo}
    )
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    created: list[dict] = []
    calls: list[dict] = []

    async def ensure_demo(school_name=None, class_name=None):
        calls.append({"school_name": school_name, "class_name": class_name})
        return demo

    monkeypatch.setattr(main.mvp, "ensure_demo_student", ensure_demo)
    signal_stubs(monkeypatch, psy, created, schools=["Школа №1"])

    # Входим как тестовый ученик.
    asyncio.run(main._handle_callback(callback_event("dev:impersonate", bot)))
    assert calls == [{"school_name": "Школа №1", "class_name": "9А"}]
    assert main._impersonation == {"42": "900001"}
    assert "Тестовый ученик (демо)" in sent[0][1]

    # Теперь ученические кнопки работают, а сигнал уходит нашему психологу.
    asyncio.run(main._handle_callback(callback_event("stress:live", bot)))
    assert created and created[0]["psychologist_id"] == psy.id

    # Выход возвращает в свой аккаунт.
    asyncio.run(main._handle_callback(callback_event("dev:impersonate_exit", bot)))
    assert main._impersonation == {}
    assert "вернулись в свой аккаунт" in sent[-1][1]


def test_impersonated_menu_shows_exit_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """В режиме «как» в меню видна кнопка выхода — забыть нельзя."""
    psy = psychologist(max_user_id="42")
    demo = student(max_user_id="900001")
    demo.id = 55
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = _two_account_bot(monkeypatch, sent, {"42": psy, "900001": demo})
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    main._impersonation["42"] = "900001"

    asyncio.run(main._handle_callback(callback_event("menu:student", bot)))

    assert sent[0][1] == "Кабинет ученика. Выберите действие:"
    assert "dev:impersonate_exit" in payloads_of(sent[0][2])


def test_dev_command_works_while_impersonating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Команда /dev проверяет настоящий ID, поэтому выход всегда доступен."""
    psy = psychologist(max_user_id="42")
    demo = student(max_user_id="900001")
    demo.id = 55
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = _two_account_bot(monkeypatch, sent, {"42": psy, "900001": demo})
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {42})
    monkeypatch.setattr(main.config, "DEV_COMMAND", "/dev")
    main._impersonation["42"] = "900001"

    event = SimpleNamespace(
        from_user=SimpleNamespace(user_id=42),
        chat=SimpleNamespace(chat_id=10),
        message=SimpleNamespace(body=SimpleNamespace(text="/dev")),
    )
    asyncio.run(main._handle_message_created(event))

    assert "Панель разработчика" in sent[0][1]
    # Панель показывает настоящий аккаунт и подсказывает, что мы «как» ученик.
    assert "900001" in sent[0][1]
    assert "dev:impersonate_exit" in payloads_of(sent[0][2])


def test_impersonation_is_not_available_to_regular_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обычный пользователь не может играть чужой аккаунт."""
    user = student()
    sent: list[tuple[int | None, str, ButtonsPayload | None, int | None]] = []
    bot = make_bot(monkeypatch, sent, user)
    monkeypatch.setattr(main.config, "DEV_MAX_IDS", {777})
    main._impersonation["42"] = "900001"

    # Чужой dev-payload игнорируется молча, а подмена не применяется.
    asyncio.run(main._handle_callback(callback_event("dev:impersonate_exit", bot)))
    assert main._impersonation == {"42": "900001"}
    assert sent == []

    assert main._effective_max_id("42") == "42"

