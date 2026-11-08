from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from maxapi import Bot
from maxapi.types import LinkButton, OpenAppButton

from app import main
from app.db.models import User
from app.keyboards.inline import menu_keyboard, mini_app_keyboard
from app.mini_web import api as mini_api
from app.mini_web.auth import make_token, read_token


# --------------------------------------------------------------------------- #
# Токены доступа
# --------------------------------------------------------------------------- #
def test_token_roundtrip() -> None:
    token = make_token(7157581)

    assert read_token(token) == 7157581


def test_tampered_token_is_rejected() -> None:
    token = make_token(42)
    payload_part, signature_part = token.rsplit(".", 1)

    assert read_token(payload_part + "." + "A" + signature_part[1:]) is None
    assert read_token("garbage") is None
    assert read_token(None) is None


def test_expired_token_is_rejected() -> None:
    token = make_token(42, ttl=-10)

    assert read_token(token) is None


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #
def test_menu_keyboard_always_offers_mini_app() -> None:
    payload = menu_keyboard("student")

    assert any(
        getattr(item, "payload", None) == "ui:miniapp"
        for row in payload.buttons
        for item in row
    )


def test_menu_keyboard_with_web_url_prepends_link_button() -> None:
    payload = menu_keyboard("teacher", web_url="https://hq.example/mini?token=x")
    first = payload.buttons[0][0]

    assert isinstance(first, LinkButton)
    assert first.url == "https://hq.example/mini?token=x"


def test_mini_app_keyboard_has_link_and_menu() -> None:
    payload = mini_app_keyboard("student", "https://x/mini")

    def payloads(p) -> list[str]:
        return [
            getattr(item, "payload", None)
            for row in p.buttons
            for item in row
            if getattr(item, "payload", None)
        ]

    assert isinstance(payload.buttons[0][0], LinkButton)
    assert payload.buttons[0][0].url == "https://x/mini"
    # Никакого выбора «основного интерфейса» — только открыть / вернуться в меню.
    assert payloads(payload) == ["menu:student"]


# --------------------------------------------------------------------------- #
# Колбэки бота
# --------------------------------------------------------------------------- #
def _callback_event(payload: str) -> object:
    from maxapi.enums.chat_type import ChatType
    from maxapi.types import Message, MessageCallback, Recipient
    from maxapi.types.callback import Callback
    from maxapi.types.users import User as MaxUser

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
    event.bot = Bot(token="test-token")
    return event


def _approved_student() -> User:
    return User(max_user_id=42, name="Иванов Иван", role="student", status="approved")


def _miniapp_callback_setup(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Общие заглушки для колбэка «🕹 Открыть мини-приложение»."""
    user = _approved_student()
    sent: list[tuple] = []

    async def find_user(max_user_id: str) -> User:
        return user

    async def send(chat_id, text, buttons=None, user_id=None) -> None:
        sent.append((chat_id, text, buttons, user_id))

    async def ack(self, callback_id: str, **kwargs: object) -> None:
        # monkeypatch подменяет метод класса, поэтому первым аргументом придёт self.
        return None

    monkeypatch.setattr(main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(Bot, "send_callback", ack)
    return sent


def test_ui_miniapp_callback_sends_link_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _miniapp_callback_setup(monkeypatch)
    # Без username бота мини-приложение открывается ссылкой с токеном.
    monkeypatch.setattr(main, "_bot_username", None)
    monkeypatch.setattr(main.config, "MINI_APP_BOT_NAME", "")

    async def no_me() -> None:
        raise RuntimeError("в тестах нет сети")

    monkeypatch.setattr(main.bot, "get_me", no_me)

    asyncio.run(main._handle_callback(_callback_event("ui:miniapp")))

    assert len(sent) == 1
    assert "Мини-приложение" in sent[0][1]
    link = sent[0][2].buttons[0][0]
    assert isinstance(link, LinkButton)
    assert "/mini?token=" in link.url
    assert read_token(link.url.split("token=")[1]) == 42


def test_ui_miniapp_callback_sends_open_app_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _miniapp_callback_setup(monkeypatch)
    # Нативная кнопка — только когда мини-приложение зарегистрировано на
    # dev.max.ru и его username явно задан в MINI_APP_BOT_NAME.
    monkeypatch.setattr(main, "_bot_username", None)
    monkeypatch.setattr(main.config, "MINI_APP_BOT_NAME", "@testbot")

    asyncio.run(main._handle_callback(_callback_event("ui:miniapp")))

    assert len(sent) == 1
    assert "Мини-приложение" in sent[0][1]
    open_app = sent[0][2].buttons[0][0]
    assert isinstance(open_app, OpenAppButton)
    assert open_app.web_app == "testbot"
    # Токен в payload не кладём: MAX ограничивает его длину (400 proto.payload),
    # доступ приходит через web_url кнопки-ссылки.
    assert open_app.payload is None
    assert sent[0][2].buttons[1][0].payload == "menu:student"


def test_menu_button_payloads_fit_max_length_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регрессия: MAX отклоняет payload кнопки длиннее 64 символов (400 proto.payload)."""
    monkeypatch.setattr(main, "_bot_username", "testbot")
    monkeypatch.setattr(main.config, "MINI_APP_BOT_NAME", "testbot")
    user = _approved_student()

    args = main._mini_app_args(user)
    kb = menu_keyboard("teacher", **args)

    assert "/mini?token=" in args["web_url"]
    for row in kb.buttons or []:
        for btn in row:
            payload = getattr(btn, "payload", None)
            assert payload is None or len(payload) <= 64


# --------------------------------------------------------------------------- #
# JSON API мини-приложения
# --------------------------------------------------------------------------- #
def _fake_user(role: str = "student", **overrides) -> SimpleNamespace:
    base: dict = {
        "id": 1,
        "max_user_id": 42,
        "full_name": "Иванов Иван",
        "role": role,
        "class_name": "11A",
        "school_name": "Школа №1",
        "grade": 11,
        "points": 12,
        "reminders_enabled": True,
        "notify_7d": True,
        "notify_3d": True,
        "notify_1d": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake = _fake_user()

    async def find_user(max_user_id: str):
        return fake if str(max_user_id) == "42" else None

    async def fake_reward_keys(user_id: int):
        return set()

    monkeypatch.setattr(mini_api.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(mini_api.mvp, "user_reward_keys", fake_reward_keys)
    app = FastAPI()
    app.include_router(mini_api.router)
    return TestClient(app)


def _headers() -> dict:
    return {"X-Mini-Token": make_token(42)}


def _role_client(monkeypatch: pytest.MonkeyPatch, role: str, **overrides) -> TestClient:
    """Клиент мини-приложения, авторизованный под нужной ролью."""
    user = _fake_user(role, **overrides)

    async def find_user(max_user_id):
        return user if str(max_user_id) == str(user.max_user_id) else None

    async def fake_reward_keys(user_id: int):
        return set()

    monkeypatch.setattr(mini_api.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(mini_api.mvp, "user_reward_keys", fake_reward_keys)
    app = FastAPI()
    app.include_router(mini_api.router)
    return TestClient(app)


def _lesson(**overrides) -> SimpleNamespace:
    """Урок расписания; по умолчанию создан учителем из фикстуры (id=1)."""
    base: dict = {
        "id": 1,
        "user_id": 1,
        "created_by": 1,
        "day_of_week": 1,
        "time": datetime(2026, 1, 5, 8, 30).time(),
        "subject": "Математика",
        "room": "204",
        "class_name": "11A",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_api_requires_valid_token(client: TestClient) -> None:
    assert client.get("/mini/api/me").status_code == 401
    assert client.get("/mini/api/me", headers={"X-Mini-Token": "bad.token"}).status_code == 401


def test_api_me_returns_profile(client: TestClient) -> None:
    resp = client.get("/mini/api/me", headers=_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Иванов Иван"
    assert data["role"] == "student"
    assert data["points"] == 12
    # Состав интерфейса приходит вместе с профилем: фронтенд строит по нему вкладки.
    assert data["features"] == list(mini_api.ROLE_FEATURES["student"])


def test_api_homework_lists_and_completes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    hw = SimpleNamespace(
        id=5, subject="Алгебра", description="№12",
        deadline=datetime(2026, 10, 30, 23, 59), class_name="11a",
        user_id=2, done=False, points=15,
    )

    async def active_homework(user):
        return [(hw, False)]

    monkeypatch.setattr(mini_api.mvp, "active_homework", active_homework)

    resp = client.get("/mini/api/homework", headers=_headers())
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["id"] == 5
    assert items[0]["deadline"] == "2026-10-30T23:59"
    assert items[0]["done"] is False
    # Баллы за сдачу задаёт учитель — фронтенд показывает их в карточке.
    assert items[0]["points"] == 15

    # Ученик не может отметить готовность — это право учительницы.
    resp = client.post(
        "/mini/api/homework/5/mark",
        headers=_headers(),
        json={"student_id": 2, "done": True},
    )
    assert resp.status_code == 403
    resp = client.get("/mini/api/homework/5/roster", headers=_headers())
    assert resp.status_code == 403


def test_api_homework_teacher_roster_and_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = _fake_user("teacher")
    student = SimpleNamespace(id=9, name="Иванов Иван")
    state = {9: False}

    async def roster(user, homework_id: int):
        assert homework_id == 5
        return [(student, state[9])]

    async def set_done(user, homework_id: int, student_id: int, done: bool) -> bool:
        state[student_id] = done
        return True

    async def assignment_points(user, homework_id: int) -> int:
        assert homework_id == 5
        return 20

    monkeypatch.setattr(mini_api.mvp, "homework_roster", roster)
    monkeypatch.setattr(mini_api.mvp, "set_homework_completion", set_done)
    monkeypatch.setattr(mini_api.mvp, "assignment_points", assignment_points)

    async def find_user(max_user_id):
        return teacher if str(max_user_id) == str(teacher.max_user_id) else None

    monkeypatch.setattr(mini_api.mvp, "user_by_max_id", find_user)
    app = FastAPI()
    app.include_router(mini_api.router)
    client = TestClient(app)

    resp = client.get("/mini/api/homework/5/roster", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["points"] == 20
    assert resp.json()["students"] == [
        {"id": 9, "name": "Иванов Иван", "done": False}
    ]

    resp = client.post(
        "/mini/api/homework/5/mark",
        headers=_headers(),
        json={"student_id": 9, "done": True},
    )
    assert resp.status_code == 200
    assert state == {9: True}

    resp = client.get("/mini/api/homework/5/roster", headers=_headers())
    assert resp.json()["students"][0]["done"] is True


def test_api_homework_create_requires_teacher(client: TestClient) -> None:
    body = {
        "class_name": "11A", "subject": "Алгебра",
        "description": "№12", "deadline": "2026-10-30T23:59",
    }

    resp = client.post("/mini/api/homework", headers=_headers(), json=body)
    assert resp.status_code == 403


def test_api_homework_create_passes_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Учитель задаёт стоимость домашки в баллах и в мини-приложении."""
    client = _role_client(monkeypatch, "teacher")
    created: list[dict] = []

    async def create_homework(owner_id, creator_id, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=7, points=kwargs["points"])

    monkeypatch.setattr(mini_api.mvp, "create_homework", create_homework)

    resp = client.post(
        "/mini/api/homework",
        headers=_headers(),
        json={
            "class_name": "11A",
            "subject": "Алгебра",
            "description": "№12",
            "deadline": "2026-10-30T23:59",
            "points": 25,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["points"] == 25
    assert created[0]["points"] == 25
    assert created[0]["class_name"] == "11A"


def test_api_homework_create_rejects_points_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _role_client(monkeypatch, "teacher")
    created: list[dict] = []

    async def create_homework(owner_id, creator_id, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=7, points=kwargs["points"])

    monkeypatch.setattr(mini_api.mvp, "create_homework", create_homework)

    for points in (150, -5):
        resp = client.post(
            "/mini/api/homework",
            headers=_headers(),
            json={
                "class_name": "11A",
                "subject": "Алгебра",
                "description": "№12",
                "deadline": "2026-10-30T23:59",
                "points": points,
            },
        )
        assert resp.status_code == 422

    assert created == []


def test_api_mood_and_settings(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[int] = []

    async def save_mood(user, score: int, comment=None) -> bool:
        saved.append(score)
        return True

    async def mood_history(user_id: int, days: int = 14):
        return []

    updates: list[dict] = []

    async def update_profile(max_user_id: str, **values):
        updates.append(values)
        return _fake_user()

    monkeypatch.setattr(mini_api.mvp, "save_mood", save_mood)
    monkeypatch.setattr(mini_api.mvp, "mood_history", mood_history)
    monkeypatch.setattr(mini_api.mvp, "update_profile", update_profile)

    resp = client.get("/mini/api/mood", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"history": []}

    resp = client.post("/mini/api/mood", headers=_headers(), json={"score": 4})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert saved == [4]

    resp = client.post("/mini/api/mood", headers=_headers(), json={"score": 9})
    assert resp.status_code == 422

    resp = client.post(
        "/mini/api/settings", headers=_headers(), json={"reminders_enabled": False}
    )
    assert resp.status_code == 200
    assert updates == [{"reminders_enabled": False}]

    # Школу и класс можно указать прямо в мини-приложении (нужно для психолога).
    resp = client.post(
        "/mini/api/settings",
        headers=_headers(),
        json={"school": "  Школа №1  ", "class_name": " 9А "},
    )
    assert resp.status_code == 200
    assert updates[-1] == {"school": "Школа №1", "class_name": "9А"}


def test_api_chat_crisis_short_circuits_ai(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_ask(message: str, key) -> str:  # pragma: no cover
        raise AssertionError("AI не должен вызываться при кризисе")

    async def fake_record(user, message):
        return None

    monkeypatch.setattr(mini_api.ai_psychologist, "ask", fail_ask)
    monkeypatch.setattr(mini_api.mvp, "record_chat_message", fake_record)

    resp = client.post(
        "/mini/api/chat", headers=_headers(), json={"message": "я хочу умереть"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["crisis"] is True
    assert "8-800-2000-122" in data["reply"]


def test_mini_page_is_served(client: TestClient) -> None:
    resp = client.get("/mini")

    assert resp.status_code == 200
    assert "Школьный штаб" in resp.text
    assert "/mini/static/mini.js" in resp.text
    # Ассеты версионируются, страница не кэшируется — иначе WebView в MAX
    # продолжит показывать старую сборку mini.js после деплоя.
    assert resp.headers["cache-control"] == "no-store"
    assert "/mini/static/mini.js?v=" in resp.text
    assert "/mini/static/mini.css?v=" in resp.text


# --------------------------------------------------------------------------- #
# Роли: состав мини-приложения и ограничения доступа
# --------------------------------------------------------------------------- #
def test_role_features_match_sections() -> None:
    assert "homework" in mini_api.ROLE_FEATURES["student"]
    assert "stress" in mini_api.ROLE_FEATURES["student"]
    # Родителю и психологу ученические разделы недоступны.
    assert "homework" not in mini_api.ROLE_FEATURES["parent"]
    assert "stress" not in mini_api.ROLE_FEATURES["parent"]
    assert "stress" not in mini_api.ROLE_FEATURES["psychologist"]
    # Расписание редактирует только учитель.
    assert "schedule_edit" in mini_api.ROLE_FEATURES["teacher"]
    assert "schedule_edit" not in mini_api.ROLE_FEATURES["student"]
    assert "signals" in mini_api.ROLE_FEATURES["psychologist"]
    assert "children" in mini_api.ROLE_FEATURES["parent"]


def test_me_returns_features_per_role(monkeypatch: pytest.MonkeyPatch) -> None:
    for role in ("student", "teacher", "parent", "psychologist"):
        client = _role_client(monkeypatch, role)

        data = client.get("/mini/api/me", headers=_headers()).json()

        assert data["role"] == role
        assert data["features"] == list(mini_api.ROLE_FEATURES[role])


def test_parent_has_no_student_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _role_client(monkeypatch, "parent")

    assert client.get("/mini/api/homework", headers=_headers()).status_code == 403
    assert client.get("/mini/api/announcements", headers=_headers()).status_code == 403
    assert client.get("/mini/api/mood", headers=_headers()).status_code == 403
    assert client.get("/mini/api/profile", headers=_headers()).status_code == 403
    assert client.get("/mini/api/signals", headers=_headers()).status_code == 403
    chat = client.post("/mini/api/chat", headers=_headers(), json={"message": "привет"})
    assert chat.status_code == 403
    reward = client.post(
        "/mini/api/rewards", headers=_headers(), json={"key": "theme_neon"}
    )
    assert reward.status_code == 403


def test_psychologist_has_no_parent_or_student_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _role_client(monkeypatch, "psychologist")

    assert client.get("/mini/api/homework", headers=_headers()).status_code == 403
    assert client.get("/mini/api/mood", headers=_headers()).status_code == 403
    assert client.get("/mini/api/children", headers=_headers()).status_code == 403
    assert client.get("/mini/api/parent/tips", headers=_headers()).status_code == 403


def test_student_cannot_touch_other_roles_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _role_client(monkeypatch, "student")

    assert client.get("/mini/api/signals", headers=_headers()).status_code == 403
    assert client.get("/mini/api/children", headers=_headers()).status_code == 403
    assert client.get("/mini/api/classes", headers=_headers()).status_code == 403
    assert client.get("/mini/api/stats", headers=_headers()).status_code == 403
    resp = client.post(
        "/mini/api/schedule",
        headers=_headers(),
        json={"class_name": "11A", "day": 1, "time": "08:30", "subject": "Химия"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Главная страница под роль
# --------------------------------------------------------------------------- #
def test_home_uses_iso_weekday(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def active_homework(user):
        return []

    async def upcoming_deadlines(user):
        return []

    async def schedule_for_day(user, weekday: int):
        captured["weekday"] = weekday
        return [_lesson()]

    async def mood_history(user_id: int, days: int = 14):
        captured["days"] = days
        return []

    monkeypatch.setattr(mini_api.mvp, "active_homework", active_homework)
    monkeypatch.setattr(mini_api.mvp, "upcoming_deadlines", upcoming_deadlines)
    monkeypatch.setattr(mini_api.mvp, "schedule_for_day", schedule_for_day)
    monkeypatch.setattr(mini_api.mvp, "mood_history", mood_history)

    data = client.get("/mini/api/home", headers=_headers()).json()

    # 1 = понедельник, как в меню бота (раньше было 0 = понедельник).
    assert captured["weekday"] == datetime.now().isoweekday()
    assert data["weekday"] == datetime.now().isoweekday()
    assert data["role"] == "student"
    assert data["lessons"][0]["time"] == "08:30"
    assert data["mood_today"] is None


def test_parent_home_lists_children(monkeypatch: pytest.MonkeyPatch) -> None:
    child = _fake_user("student", id=9, full_name="Иванов Иван", class_name="11A")
    deadline = SimpleNamespace(
        id=3, title="Контрольная", kind="контрольная", class_name="11A",
        date=datetime(2026, 10, 30, 23, 59),
    )

    async def parent_children(parent):
        return [child]

    async def parent_summary(parent, student_id=None):
        return child, 4, 1, [deadline]

    monkeypatch.setattr(mini_api.mvp, "parent_children", parent_children)
    monkeypatch.setattr(mini_api.mvp, "parent_summary", parent_summary)

    client = _role_client(monkeypatch, "parent", reminders_enabled=False)
    data = client.get("/mini/api/home", headers=_headers()).json()

    assert data["role"] == "parent"
    assert data["children_count"] == 1
    assert data["completed_week"] == 4
    assert data["overdue_total"] == 1
    assert data["children"][0]["name"] == "Иванов Иван"
    assert data["children"][0]["deadlines"][0]["date"] == "2026-10-30T23:59"
    assert data["next_deadline_title"] == "Контрольная"
    assert data["next_deadline_child"] == "Иванов Иван"
    assert data["notifications_enabled"] is False
    # Ученических блоков у родителя нет.
    assert "points" not in data
    assert "lessons" not in data


def test_psychologist_home_shows_urgent_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = _fake_user("student", id=9, full_name="Петров Пётр", class_name="11A")
    signal = SimpleNamespace(
        id=1, student=student, risk_level=3, themes="Тревога, Сон",
        status="pending", preferred_format="Лично",
        created_at=datetime(2026, 10, 25, 9, 0),
    )

    async def psychologist_signals(psychologist_id: int):
        return [signal]

    async def psychologist_signals_summary(psychologist_id: int) -> dict:
        return {"total": 2, "pending": 1, "in_progress": 1, "closed": 0}

    monkeypatch.setattr(mini_api.mvp, "psychologist_signals", psychologist_signals)
    monkeypatch.setattr(
        mini_api.mvp, "psychologist_signals_summary", psychologist_signals_summary
    )

    client = _role_client(monkeypatch, "psychologist")
    data = client.get("/mini/api/home", headers=_headers()).json()

    assert data["role"] == "psychologist"
    assert data["summary"]["pending"] == 1
    assert data["urgent"][0]["student_name"] == "Петров Пётр"
    assert data["urgent"][0]["status_label"] == "ожидает"
    assert data["urgent"][0]["risk_level"] == 3
    assert "points" not in data


# --------------------------------------------------------------------------- #
# Расписание: ученик смотрит, учитель редактирует
# --------------------------------------------------------------------------- #
def test_schedule_is_read_only_for_student(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def schedule_for_day(user, weekday: int):
        return [_lesson(created_by=99)] if weekday == 1 else []

    monkeypatch.setattr(mini_api.mvp, "schedule_for_day", schedule_for_day)

    data = client.get("/mini/api/schedule", headers=_headers()).json()

    assert data["editable"] is False
    assert data["classes"] == []
    assert data["class_name"] == "11A"
    assert sorted(data["days"], key=int) == ["1", "2", "3", "4", "5", "6", "7"]
    assert data["days"]["1"][0]["can_edit"] is False
    assert data["days"]["2"] == []

    # Один день — по явному номеру (ISO: 7 = воскресенье).
    assert client.get("/mini/api/schedule?day=7", headers=_headers()).status_code == 200
    assert client.get("/mini/api/schedule?day=0", headers=_headers()).status_code == 422


def test_teacher_schedule_lists_class_lessons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def teacher_classes(user_id: int):
        return [
            SimpleNamespace(name="11A", members_count=3, login="l11a", password="p1"),
            SimpleNamespace(name="10B", members_count=2, login="l10b", password="p2"),
        ]

    async def schedule_for_class(class_name, school, weekday=None):
        if weekday != 1:
            return []
        return [
            _lesson(),
            _lesson(
                id=2, created_by=77, subject="Физика",
                time=datetime(2026, 1, 5, 9, 20).time(),
            ),
        ]

    monkeypatch.setattr(mini_api.crud, "classes_for_teacher", teacher_classes)
    monkeypatch.setattr(mini_api.mvp, "schedule_for_class", schedule_for_class)

    client = _role_client(monkeypatch, "teacher")
    data = client.get("/mini/api/schedule", headers=_headers()).json()

    assert data["role"] == "teacher"
    assert data["editable"] is True
    assert data["classes"] == ["11A", "10B"]
    assert data["class_name"] == "11A"
    mine, other = data["days"]["1"]
    assert mine["can_edit"] is True
    assert other["can_edit"] is False
    assert other["time"] == "09:20"

    # Чужой класс смотреть нельзя, свой — можно.
    assert client.get("/mini/api/schedule?class_name=9Z", headers=_headers()).status_code == 403
    own = client.get("/mini/api/schedule?class_name=10B", headers=_headers()).json()
    assert own["class_name"] == "10B"


def test_teacher_without_classes_gets_empty_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def teacher_classes(user_id: int):
        return []

    monkeypatch.setattr(mini_api.crud, "classes_for_teacher", teacher_classes)

    client = _role_client(monkeypatch, "teacher", class_name=None)
    data = client.get("/mini/api/schedule", headers=_headers()).json()

    assert data["class_name"] is None
    assert data["editable"] is True
    assert data["days"]["1"] == []

    # Новый урок без класса отклоняется.
    resp = client.post(
        "/mini/api/schedule", headers=_headers(),
        json={"day": 1, "time": "08:30", "subject": "Химия"},
    )
    assert resp.status_code == 422


def test_teacher_schedule_create_update_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict] = []
    updated: list[dict] = []
    deleted: list[int] = []

    async def teacher_classes(user_id: int):
        return [SimpleNamespace(name="11A", members_count=3, login="l11a", password="p1")]

    async def create_for_class(teacher, class_name, day, lesson_time, subject, room):
        created.append(
            {"class": class_name, "day": day, "time": lesson_time,
             "subject": subject, "room": room}
        )
        return _lesson(id=5, day_of_week=day, time=lesson_time, subject=subject, room=room)

    async def update_item(
        teacher_id, item_id, *, day_of_week=None, lesson_time=None, subject=None, room=None
    ):
        updated.append(
            {"teacher": teacher_id, "id": item_id, "day": day_of_week,
             "time": lesson_time, "subject": subject, "room": room}
        )
        if item_id != 1:  # чужой или отсутствующий урок
            return None
        return _lesson(
            id=item_id,
            day_of_week=day_of_week or 1,
            time=lesson_time or datetime(2026, 1, 5, 8, 30).time(),
            subject=subject or "Математика",
            room=room,
        )

    async def delete_item(user_id, item_id):
        if item_id != 1:
            return False
        deleted.append(item_id)
        return True

    monkeypatch.setattr(mini_api.crud, "classes_for_teacher", teacher_classes)
    monkeypatch.setattr(mini_api.mvp, "create_user_schedule_for_class", create_for_class)
    monkeypatch.setattr(mini_api.mvp, "update_schedule_item", update_item)
    monkeypatch.setattr(mini_api.mvp, "delete_schedule_item", delete_item)

    client = _role_client(monkeypatch, "teacher")

    # Создание: день можно передать текстом («пт»), время — «0915».
    resp = client.post(
        "/mini/api/schedule", headers=_headers(),
        json={"class_name": "11A", "day": "пт", "time": "0915",
              "subject": "Химия", "room": "12"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 5}
    assert created[0]["class"] == "11A"
    assert created[0]["day"] == 5
    assert created[0]["time"].strftime("%H:%M") == "09:15"
    assert created[0]["room"] == "12"

    # Пустой предмет, неверный день и неверное время отклоняются.
    for body in (
        {"class_name": "11A", "day": 1, "time": "09:15", "subject": "  "},
        {"class_name": "11A", "day": 9, "time": "09:15", "subject": "Химия"},
        {"class_name": "11A", "day": 1, "time": "25:99", "subject": "Химия"},
        {"class_name": "9Z", "day": 1, "time": "09:15", "subject": "Химия"},
    ):
        assert client.post("/mini/api/schedule", headers=_headers(), json=body).status_code in (403, 422)
    assert len(created) == 1

    # Правка своего урока.
    resp = client.patch(
        "/mini/api/schedule/1", headers=_headers(), json={"time": "10:00", "room": "301"}
    )
    assert resp.status_code == 200
    item = resp.json()["item"]
    assert item["time"] == "10:00"
    assert item["room"] == "301"
    assert updated[0]["time"].strftime("%H:%M") == "10:00"
    assert updated[0]["subject"] is None
    assert updated[0]["teacher"] == 1

    # Чужой урок изменить нельзя.
    resp = client.patch("/mini/api/schedule/2", headers=_headers(), json={"time": "11:00"})
    assert resp.status_code == 404
    assert updated[-1]["id"] == 2

    # Удаление своего урока.
    resp = client.delete("/mini/api/schedule/1", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert deleted == [1]

    # Чужой урок не удаляется.
    assert client.delete("/mini/api/schedule/2", headers=_headers()).status_code == 404
    assert deleted == [1]


# --------------------------------------------------------------------------- #
# Родитель: дети, сводка, советы
# --------------------------------------------------------------------------- #
def _parent_fixtures(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Общие заглушки для родительских разделов."""
    child = _fake_user("student", id=9, full_name="Иванов Иван", class_name="11A")
    deadline = SimpleNamespace(
        id=3, title="Контрольная", kind="контрольная", class_name="11A",
        date=datetime(2026, 10, 30, 23, 59),
    )
    requested: list = []

    async def parent_children(parent):
        return [child]

    async def parent_summary(parent, student_id=None):
        requested.append(student_id)
        return child, 4, 1, [deadline]

    async def deadlines_for_parent(parent):
        return [deadline]

    monkeypatch.setattr(mini_api.mvp, "parent_children", parent_children)
    monkeypatch.setattr(mini_api.mvp, "parent_summary", parent_summary)
    monkeypatch.setattr(mini_api.mvp, "deadlines_for_parent", deadlines_for_parent)
    return SimpleNamespace(child=child, deadline=deadline, requested=requested)


def test_parent_children_summary_and_tips(monkeypatch: pytest.MonkeyPatch) -> None:
    _parent_fixtures(monkeypatch)
    client = _role_client(monkeypatch, "parent")

    data = client.get("/mini/api/children", headers=_headers()).json()
    assert data["items"][0]["name"] == "Иванов Иван"
    assert data["items"][0]["class_name"] == "11A"
    assert data["items"][0]["completed_week"] == 4
    assert data["items"][0]["overdue"] == 1
    assert data["items"][0]["deadlines"][0]["title"] == "Контрольная"
    assert data["notifications_enabled"] is True

    summary = client.get("/mini/api/parent/summary?child_id=9", headers=_headers()).json()
    assert summary["child"]["name"] == "Иванов Иван"
    assert summary["child"]["class_name"] == "11A"
    assert summary["completed_week"] == 4
    assert summary["overdue"] == 1
    assert summary["deadlines"][0]["date"] == "2026-10-30T23:59"

    tips = client.get("/mini/api/parent/tips", headers=_headers()).json()
    assert tips["items"] == list(mini_api.PARENT_TIPS)
    assert all(tip.strip() for tip in tips["items"])


def test_parent_deadlines_uses_parent_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _parent_fixtures(monkeypatch)

    async def fail_upcoming(user):  # pragma: no cover
        raise AssertionError("Родитель не должен читать ученические дедлайны")

    monkeypatch.setattr(mini_api.mvp, "upcoming_deadlines", fail_upcoming)

    client = _role_client(monkeypatch, "parent")
    data = client.get("/mini/api/deadlines", headers=_headers()).json()

    assert [item["title"] for item in data["items"]] == ["Контрольная"]


def test_parent_without_linked_child(monkeypatch: pytest.MonkeyPatch) -> None:
    async def parent_children(parent):
        return []

    async def parent_summary(parent, student_id=None):
        return None, 0, 0, []

    monkeypatch.setattr(mini_api.mvp, "parent_children", parent_children)
    monkeypatch.setattr(mini_api.mvp, "parent_summary", parent_summary)

    client = _role_client(monkeypatch, "parent")

    assert client.get("/mini/api/children", headers=_headers()).json()["items"] == []
    data = client.get("/mini/api/parent/summary", headers=_headers()).json()
    assert data["child"] is None
    assert data["completed_week"] == 0
    assert data["deadlines"] == []
    home = client.get("/mini/api/home", headers=_headers()).json()
    assert home["children_count"] == 0
    assert home["next_deadline"] is None


# --------------------------------------------------------------------------- #
# Психолог: сигналы учеников
# --------------------------------------------------------------------------- #
def test_psychologist_signals_list_and_status_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = _fake_user("student", id=9, full_name="Петров Пётр", class_name="11A")
    signal = SimpleNamespace(
        id=1, student=student, risk_level=2, themes="Тревога, Сон",
        status="pending", preferred_format="Онлайн",
        created_at=datetime(2026, 10, 25, 9, 0),
    )
    changes: list[tuple[int, str]] = []

    async def psychologist_signals(psychologist_id: int):
        return [signal]

    async def psychologist_signals_summary(psychologist_id: int) -> dict:
        return {"total": 1, "pending": 1, "in_progress": 0, "closed": 0}

    async def psychologist_signal(psychologist_id: int, signal_id: int):
        return signal if signal_id == 1 else None

    async def update_signal_status(signal_id: int, status: str) -> bool:
        changes.append((signal_id, status))
        signal.status = status
        return True

    monkeypatch.setattr(mini_api.mvp, "psychologist_signals", psychologist_signals)
    monkeypatch.setattr(
        mini_api.mvp, "psychologist_signals_summary", psychologist_signals_summary
    )
    monkeypatch.setattr(mini_api.mvp, "psychologist_signal", psychologist_signal)
    monkeypatch.setattr(mini_api.mvp, "update_signal_status", update_signal_status)

    client = _role_client(monkeypatch, "psychologist")

    data = client.get("/mini/api/signals", headers=_headers()).json()
    assert data["summary"]["pending"] == 1
    assert data["items"][0]["student_name"] == "Петров Пётр"
    assert data["items"][0]["class_name"] == "11A"
    assert data["items"][0]["themes"] == "Тревога, Сон"
    assert data["items"][0]["preferred_format"] == "Онлайн"
    assert data["items"][0]["created_at"] == "2026-10-25T09:00"
    assert [item["key"] for item in data["statuses"]] == [
        "pending", "in_progress", "closed"
    ]

    resp = client.post(
        "/mini/api/signals/1", headers=_headers(), json={"status": "in_progress"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True, "status": "in_progress", "status_label": "в работе"
    }
    assert changes == [(1, "in_progress")]

    # Неизвестный статус и чужой сигнал отклоняются.
    bad = client.post(
        "/mini/api/signals/1", headers=_headers(), json={"status": "done"}
    )
    assert bad.status_code == 422
    missing = client.post(
        "/mini/api/signals/99", headers=_headers(), json={"status": "closed"}
    )
    assert missing.status_code == 404
    assert changes == [(1, "in_progress")]


# --------------------------------------------------------------------------- #
# Ученик: передача сигнала живому психологу из мини-приложения
# --------------------------------------------------------------------------- #
def _psy_stubs(
    monkeypatch: pytest.MonkeyPatch,
    psy: SimpleNamespace | None,
    schools: list[str],
    created: list[tuple],
    duty: SimpleNamespace | None = None,
) -> None:
    async def for_school(school_name: str):
        return psy

    async def school_list():
        return schools

    async def any_psy():
        return duty if duty is not None else psy

    async def last_message(user):
        return None

    async def open_signal(student_id: int, psychologist_id: int) -> bool:
        # Дубликатов сигналов нет — каждый вызов создаёт новый.
        return False

    async def snapshot(user):
        # Риск/темы для сигнала: пустая история → заметный риск по умолчанию.
        return 4, []

    async def create_signal(student_id, psychologist_id, **kwargs):
        created.append((student_id, psychologist_id, kwargs))
        return SimpleNamespace(
            id=21, risk_level=kwargs["risk_level"], themes=kwargs["themes"]
        )

    monkeypatch.setattr(mini_api.mvp, "psychologist_for_school", for_school)
    monkeypatch.setattr(mini_api.mvp, "psychologist_schools", school_list)
    monkeypatch.setattr(mini_api.mvp, "any_psychologist", any_psy)
    monkeypatch.setattr(mini_api.mvp, "last_chat_message", last_message)
    monkeypatch.setattr(mini_api.mvp, "has_open_signal", open_signal)
    monkeypatch.setattr(mini_api.mvp, "stress_snapshot", snapshot)
    monkeypatch.setattr(mini_api.mvp, "create_stress_signal", create_signal)
    # Уведомление психологу в MAX в тестах не отправляем.
    monkeypatch.setattr(mini_api, "BOT_TOKEN", "")


def test_api_psychologist_info_for_student(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _role_client(monkeypatch, "student")
    psy = SimpleNamespace(id=3, max_user_id=99, full_name="Петрова Анна")
    _psy_stubs(monkeypatch, psy, ["Школа №1"], [])

    resp = client.get("/mini/api/psychologist", headers=_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["school_set"] is True
    assert data["schools"] == ["Школа №1"]
    assert "8-800-2000-122" in data["crisis"]


def test_api_signal_create_reaches_school_psychologist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _role_client(monkeypatch, "student")
    psy = SimpleNamespace(id=3, max_user_id=99, full_name="Петрова Анна")
    created: list[tuple] = []
    _psy_stubs(monkeypatch, psy, ["Школа №1"], created)

    resp = client.post(
        "/mini/api/signals", headers=_headers(), json={"preferred_format": "live"}
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["id"] == 21
    assert created == [(1, 3, {"risk_level": 4, "themes": [], "preferred_format": "live", "consent": True})]


def test_api_signal_offers_duty_psychologist_only_with_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _role_client(monkeypatch, "student", school_name="Школа №99")
    duty = SimpleNamespace(id=4, max_user_id=77, full_name="Дежурный")
    created: list[tuple] = []

    async def for_school(school_name: str):
        return None

    _psy_stubs(monkeypatch, None, ["Школа №1"], created, duty=duty)
    monkeypatch.setattr(mini_api.mvp, "psychologist_for_school", for_school)

    resp = client.post(
        "/mini/api/signals", headers=_headers(), json={"preferred_format": "chat"}
    )
    data = resp.json()
    assert data["ok"] is False
    assert data["reason"] == "no_psychologist"
    assert data["duty_available"] is True
    assert created == []

    resp = client.post(
        "/mini/api/signals",
        headers=_headers(),
        json={"preferred_format": "chat", "allow_duty": True},
    )
    assert resp.json()["ok"] is True
    assert resp.json()["duty"] is True
    assert created == [(1, 4, {"risk_level": 4, "themes": [], "preferred_format": "chat", "consent": True})]


def test_api_signal_requires_school(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _role_client(monkeypatch, "student", school_name=None)
    _psy_stubs(monkeypatch, None, ["Школа №1"], [])

    resp = client.post(
        "/mini/api/signals", headers=_headers(), json={"preferred_format": "chat"}
    )

    assert resp.json()["ok"] is False
    assert resp.json()["reason"] == "no_school"


def test_api_signal_rejects_bad_format(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _role_client(monkeypatch, "student")
    _psy_stubs(monkeypatch, None, [], [])

    resp = client.post(
        "/mini/api/signals", headers=_headers(), json={"preferred_format": "sms"}
    )

    assert resp.status_code == 422


def test_api_signal_is_student_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Родитель и психолог не могут отправлять сигнал от имени ученика."""
    for role in ("parent", "psychologist", "teacher"):
        client = _role_client(monkeypatch, role)
        _psy_stubs(monkeypatch, None, [], [])
        resp = client.post(
            "/mini/api/signals", headers=_headers(), json={"preferred_format": "chat"}
        )
        assert resp.status_code == 403, role
