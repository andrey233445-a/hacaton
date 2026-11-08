from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db.models import RequestStatus, SchoolClass, User, UserRole
from app import main
from app.mini_web.api import _parse_dt, router
from app.mini_web.auth import make_token
from app.services import scheduler
from fastapi import FastAPI


def test_onboarding_classes_grade_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(
        id=1,
        max_user_id="100",
        role=UserRole.STUDENT,
        status=RequestStatus.APPROVED,
        grade=9,
    )
    saved: dict[str, str] = {}

    async def mock_update_profile(max_id: str, **kwargs: str) -> User:
        saved.update(kwargs)
        return user

    async def mock_set_state(u: User, step: str | None, **kwargs: object) -> None:
        pass

    async def mock_menu(chat_id: int | None, u: User, max_id: int | str) -> None:
        pass

    monkeypatch.setattr(main.mvp, "update_profile", mock_update_profile)
    monkeypatch.setattr(main, "_set_state", mock_set_state)
    monkeypatch.setattr(main, "_menu", mock_menu)

    # Student enters just the letter "а"
    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=100,
            user=user,
            step="onboarding_classes",
            text="а",
            state={},
        )
    )
    assert handled is True
    assert saved["classes"] == "9А"
    assert saved["class_name"] == "9А"

    # Student enters full class "9б" -> no duplicated grade prefix
    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=100,
            user=user,
            step="onboarding_classes",
            text="9б",
            state={},
        )
    )
    assert handled is True
    assert saved["classes"] == "9б"


def test_parse_dt_various_formats() -> None:
    dt1 = _parse_dt("2026-11-03T15:30:00")
    assert dt1.year == 2026 and dt1.month == 11 and dt1.hour == 15

    dt2 = _parse_dt("03.11.2026 15:30")
    assert dt2.year == 2026 and dt2.month == 11 and dt2.day == 3 and dt2.hour == 15 and dt2.minute == 30

    dt3 = _parse_dt("03.11.2026")
    assert dt3.year == 2026 and dt3.month == 11 and dt3.day == 3

    with pytest.raises(HTTPException) as exc_info:
        _parse_dt("not-a-date")
    assert exc_info.value.status_code == 422


def test_api_join_class_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    user = User(
        id=2,
        max_user_id="200",
        role=UserRole.STUDENT,
        status=RequestStatus.APPROVED,
        full_name="Тест Ученик",
    )

    async def mock_user_by_max_id(max_id: str) -> User | None:
        return user if str(max_id) == "200" else None

    async def mock_join_class(student_id: int, login: str, password: str) -> str:
        raise ValueError("Неверный логин или пароль класса.")

    monkeypatch.setattr("app.mini_web.api.mvp.user_by_max_id", mock_user_by_max_id)
    monkeypatch.setattr("app.mini_web.api.crud.join_school_class", mock_join_class)
    monkeypatch.setattr("app.mini_web.api.mvp.user_reward_keys", AsyncMock(return_value=set()))

    token = make_token("200")
    response = client.post(
        "/mini/api/join_class",
        headers={"X-Mini-Token": token},
        json={"login": "bad_login", "password": "wrong_password"},
    )
    assert response.status_code == 400
    assert "Неверный логин или пароль класса." in response.json()["detail"]


def test_api_classes_decrypt_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    teacher = User(
        id=3,
        max_user_id="300",
        role=UserRole.TEACHER,
        status=RequestStatus.APPROVED,
        full_name="Учитель Тест",
    )
    school_class = SchoolClass(
        id=10,
        teacher_id=3,
        name="9А",
        login="class_9a",
        password_hash="hash",
        password_encrypted="corrupted_or_no_secret",
    )

    async def mock_user_by_max_id(max_id: str) -> User | None:
        return teacher if str(max_id) == "300" else None

    async def mock_classes_for_teacher(teacher_id: int) -> list[SchoolClass]:
        return [school_class]

    def mock_decrypt(value: str) -> str:
        raise RuntimeError("Шифрование паролей не настроено.")

    monkeypatch.setattr("app.mini_web.api.mvp.user_by_max_id", mock_user_by_max_id)
    monkeypatch.setattr("app.mini_web.api.crud.classes_for_teacher", mock_classes_for_teacher)
    monkeypatch.setattr("app.mini_web.api.crud.decrypt_class_password", mock_decrypt)
    monkeypatch.setattr("app.mini_web.api.mvp.user_reward_keys", AsyncMock(return_value=set()))

    # Mock count query in session
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def scalar(self, *args, **kwargs):
            return 5

    monkeypatch.setattr("app.mini_web.api.async_session", lambda: FakeSession())

    token = make_token("300")
    response = client.get("/mini/api/classes", headers={"X-Mini-Token": token})
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["password"] == "—"


def test_api_deadline_create_notifies_parents(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    teacher = User(
        id=4,
        max_user_id="400",
        role=UserRole.TEACHER,
        status=RequestStatus.APPROVED,
        school="Школа №1",
    )

    async def mock_user_by_max_id(max_id: str) -> User | None:
        return teacher if str(max_id) == "400" else None

    class FakeDeadline:
        id = 99
        title = "Контрольная работа"
        due = datetime(2026, 11, 10, 10, 0)
        kind = "контрольная"

    notified: list[tuple] = []

    async def mock_create_deadline(*args, **kwargs):
        return FakeDeadline()

    async def mock_notify_parents(class_name, user, item, kind, due):
        notified.append((class_name, user.id, item.id, kind, due))

    monkeypatch.setattr("app.mini_web.api.mvp.user_by_max_id", mock_user_by_max_id)
    monkeypatch.setattr("app.mini_web.api.mvp.create_deadline", mock_create_deadline)
    monkeypatch.setattr("app.main._notify_parents_about_deadline", mock_notify_parents)
    monkeypatch.setattr("app.mini_web.api.mvp.user_reward_keys", AsyncMock(return_value=set()))

    token = make_token("400")
    response = client.post(
        "/mini/api/deadlines",
        headers={"X-Mini-Token": token},
        json={
            "title": "Контрольная работа",
            "date": "2026-11-10T10:00:00",
            "kind": "контрольная",
            "class_name": "9А",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 99}
    assert len(notified) == 1
    assert notified[0][0] == "9А"


def test_scheduler_non_integer_max_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    user = User(
        id=5,
        max_user_id="not_an_int",
        role=UserRole.STUDENT,
        reminders_enabled=True,
    )

    async def mock_reminder_users():
        return [user]

    async def mock_build_student_reminders(u, now):
        return [("key1", "Текст напоминания")]

    async def mock_reminder_already_delivered(uid, key):
        return False

    sent_targets: list[int] = []

    async def mock_sender(user_id: int, text: str) -> None:
        sent_targets.append(user_id)

    monkeypatch.setattr(scheduler.mvp, "reminder_users", mock_reminder_users)
    monkeypatch.setattr(scheduler, "build_student_reminders", mock_build_student_reminders)
    monkeypatch.setattr(scheduler.mvp, "reminder_already_delivered", mock_reminder_already_delivered)

    count = asyncio.run(scheduler.run_once(mock_sender, datetime(2026, 11, 3, 12, 0)))
    # Non-integer ID was skipped safely without throwing exception
    assert count == 0
    assert len(sent_targets) == 0
