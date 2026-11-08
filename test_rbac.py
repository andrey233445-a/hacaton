from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from maxapi.types import Attachment
from sqlalchemy import BigInteger, UniqueConstraint

from app.admin_web import main as admin
from app.db import crud
from app.db.crud import LinkDecision, hash_class_password, verify_class_password
from app.db.models import (
    ClassMember,
    ParentStudentLink,
    RequestStatus,
    SchoolClass,
    User,
    UserRole,
)
from app import main as bot_main


def test_send_wraps_inline_keyboard_in_max_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    async def send_message(**kwargs: object) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(bot_main.bot, "send_message", send_message)
    buttons = bot_main.keyboard((("Open", "open"),))

    asyncio.run(bot_main._send(123, "Menu", buttons))

    attachments = sent[0]["attachments"]
    assert isinstance(attachments, list)
    assert len(attachments) == 1
    attachment = attachments[0]
    assert isinstance(attachment, Attachment)
    assert attachment.model_dump(mode="json", exclude_none=True) == {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [
                    {
                        "type": "callback",
                        "text": "Open",
                        "payload": "open",
                        "intent": "default",
                    }
                ]
            ]
        },
    }


def test_class_password_is_salted_encrypted_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = hash_class_password("secure-class-password")
    second = hash_class_password("secure-class-password")
    monkeypatch.setattr(
        crud,
        "CLASS_PASSWORD_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    encrypted = crud.encrypt_class_password("secure-class-password")

    assert first != second
    assert "secure-class-password" not in first
    assert "secure-class-password" not in encrypted
    assert crud.decrypt_class_password(encrypted) == "secure-class-password"
    assert verify_class_password("secure-class-password", first)
    assert not verify_class_password("wrong-password", first)


def test_user_registration_defaults_to_active_for_every_role() -> None:
    """Администратора нет: любой аккаунт активен сразу после регистрации."""
    student = User(max_user_id=101, role=UserRole.STUDENT)
    teacher = User(max_user_id=102, role=UserRole.TEACHER)
    psychologist = User(max_user_id=103, role=UserRole.PSYCHOLOGIST)

    assert student.status == RequestStatus.APPROVED
    assert teacher.status == RequestStatus.APPROVED
    assert psychologist.status == RequestStatus.APPROVED
    assert student.role == "student"
    assert student.reminders_enabled is True


def test_rbac_schema_has_numeric_ids_and_unique_many_to_many_pairs() -> None:
    assert isinstance(User.__table__.c.max_user_id.type, BigInteger)
    assert "full_name" in User.__table__.c
    assert "school_name" in User.__table__.c
    class_member_constraints = {
        constraint.name
        for constraint in ClassMember.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    parent_link_constraints = {
        constraint.name
        for constraint in ParentStudentLink.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "uq_class_member" in class_member_constraints
    assert "uq_parent_student_link" in parent_link_constraints
    assert SchoolClass.__table__.c.password_hash.nullable is False
    assert SchoolClass.__table__.c.password_encrypted.nullable is False


def test_admin_authentication_requires_configured_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin, "ADMIN_USERNAME", "school-admin")
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", "test-password")

    assert admin.require_admin(
        HTTPBasicCredentials(username="school-admin", password="test-password")
    ) == "school-admin"

    with pytest.raises(HTTPException) as error:
        admin.require_admin(
            HTTPBasicCredentials(username="school-admin", password="incorrect")
        )
    assert error.value.status_code == 401

    monkeypatch.setattr(admin, "ADMIN_PASSWORD", None)
    with pytest.raises(HTTPException) as error:
        admin.require_admin(
            HTTPBasicCredentials(username="school-admin", password="test-password")
        )
    assert error.value.status_code == 503


def test_admin_accounts_page_search_and_role_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = User(
        id=12,
        max_user_id=123456,
        name="Анна Петрова",
        school="Школа №1",
        role=UserRole.STUDENT,
        status=RequestStatus.APPROVED,
    )
    listed: list[dict[str, object]] = []
    changed: list[tuple[int, UserRole]] = []

    async def list_test_accounts(**kwargs: object) -> tuple[list[User], int]:
        listed.append(kwargs)
        return [account], 1

    async def change_test_role(user_id: int, role: UserRole) -> User:
        changed.append((user_id, role))
        return account

    monkeypatch.setattr(admin, "list_accounts", list_test_accounts)
    monkeypatch.setattr(admin, "change_account_role", change_test_role)
    admin.app.dependency_overrides[admin.require_admin] = lambda: "admin"
    try:
        with TestClient(admin.app) as client:
            response = client.get(
                "/admin/accounts",
                params={"search": "Анна", "role_filter": "student"},
            )
            assert response.status_code == 200
            assert "Анна Петрова" in response.text
            assert "Зарегистрированные аккаунты" in response.text
            assert listed[0]["search"] == "Анна"
            assert listed[0]["role_filter"] == "student"

            response = client.post(
                "/admin/accounts/12/role?search=%D0%90%D0%BD%D0%BD%D0%B0&page=2",
                data={"role": "teacher", "search": "Анна", "page": "2"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"].endswith("search=%D0%90%D0%BD%D0%BD%D0%B0&page=2")
            assert changed == [(12, UserRole.TEACHER)]
    finally:
        admin.app.dependency_overrides.pop(admin.require_admin, None)


def test_admin_account_delete_requires_confirmation_and_same_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[int] = []

    async def delete_test_account(user_id: int) -> bool:
        deleted.append(user_id)
        return True

    monkeypatch.setattr(admin, "delete_account", delete_test_account)
    admin.app.dependency_overrides[admin.require_admin] = lambda: "admin"
    try:
        with TestClient(admin.app) as client:
            denied = client.post(
                "/admin/accounts/12/delete",
                data={"confirm": "delete"},
                follow_redirects=False,
            )
            assert denied.status_code == 403
            assert not deleted

            denied = client.post(
                "/admin/accounts/12/delete",
                data={"confirm": "no"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            assert denied.status_code == 400
            assert not deleted

            response = client.post(
                "/admin/accounts/12/delete",
                data={"confirm": "delete"},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert deleted == [12]
    finally:
        admin.app.dependency_overrides.pop(admin.require_admin, None)


def test_legacy_pending_teacher_is_activated_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Старый статус «ожидает» больше не блокирует: аккаунт активируется сам."""
    teacher = User(
        id=7,
        max_user_id=700,
        role=UserRole.TEACHER,
        status=RequestStatus.PENDING,
    )
    sent: list[str] = []
    activated: list[str] = []

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "700"
        return teacher

    async def approve_user(max_user_id: str) -> User:
        activated.append(max_user_id)
        teacher.status = RequestStatus.APPROVED
        return teacher

    async def send(
        chat_id: int | None,
        text: str,
        buttons: object | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def acknowledge(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(bot_main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(bot_main.mvp, "approve_user", approve_user)
    monkeypatch.setattr(bot_main, "_send", send)
    event = SimpleNamespace(
        callback=SimpleNamespace(
            callback_id="callback-id",
            payload="teacher:create_class",
            user=SimpleNamespace(user_id=700),
        ),
        message=SimpleNamespace(
            recipient=SimpleNamespace(chat_id=10),
        ),
        bot=SimpleNamespace(send_callback=acknowledge),
    )

    asyncio.run(bot_main._handle_callback(event))

    assert activated == ["700"]
    assert teacher.status == RequestStatus.APPROVED
    # Доступ открыт сразу: учитель продолжает создание класса.
    assert sent == ["Введите название класса, например 10Б."]
    bot_main._states.pop("700", None)


def test_role_button_callback_is_acknowledged_and_starts_onboarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=7,
        max_user_id=700,
        name="Иванов Иван Иванович",
        role=UserRole.TEACHER,
        status=RequestStatus.APPROVED,
    )
    acknowledgements: list[dict[str, object]] = []
    sent: list[str] = []

    async def find_user(max_user_id: str) -> None:
        assert max_user_id == "700"
        return None

    async def update_profile(max_user_id: str, **values: object) -> User:
        assert max_user_id == "700"
        # Администратора нет: роль выбирается и активируется сразу.
        assert values == {"role": "teacher", "status": "approved"}
        return user

    async def send(
        chat_id: int | None,
        text: str,
        buttons: object | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append(text)

    async def acknowledge(**kwargs: object) -> None:
        acknowledgements.append(kwargs)

    monkeypatch.setattr(bot_main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(bot_main.mvp, "update_profile", update_profile)
    monkeypatch.setattr(bot_main, "_send", send)
    event = SimpleNamespace(
        callback=SimpleNamespace(
            callback_id="callback-id",
            payload="onboard:role:teacher",
            user=SimpleNamespace(user_id=700),
        ),
        message=SimpleNamespace(
            recipient=SimpleNamespace(chat_id=10),
        ),
        bot=SimpleNamespace(send_callback=acknowledge),
    )

    asyncio.run(bot_main._handle_callback(event))

    assert acknowledgements == [
        {"callback_id": "callback-id", "notification": "Запрос принят."}
    ]
    assert sent == ["В какой школе работаешь?"]
    assert bot_main._get_state("700") == {"step": "onboarding_school"}
    bot_main._states.pop("700", None)


@pytest.mark.parametrize(
    ("payload", "approve", "expected_student_text"),
    [
        (
            "link_approve:12",
            True,
            "Вы подтвердили привязку родителя Родитель.",
        ),
        (
            "link_reject:12",
            False,
            "Вы отклонили запрос родителя Родитель.",
        ),
    ],
)
def test_parent_link_callback_edits_the_student_message(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    approve: bool,
    expected_student_text: str,
) -> None:
    student = User(
        id=9,
        max_user_id=700,
        role=UserRole.STUDENT,
        status=RequestStatus.APPROVED,
    )
    edited: list[tuple[str, list[object]]] = []
    sent: list[tuple[int | None, str, int | None]] = []
    should_approve = approve

    async def find_user(max_user_id: str) -> User:
        assert max_user_id == "700"
        return student

    async def resolve_link(
        link_id: int, student_max_user_id: str, approve: bool
    ) -> LinkDecision:
        assert link_id == 12
        assert student_max_user_id == "700"
        assert approve is should_approve
        return LinkDecision(
            status="approved" if approve else "rejected",
            parent_name="Родитель",
            parent_max_user_id=800,
            student_name="Ученик",
        )

    async def edit_message(text: str, attachments: list[object]) -> None:
        edited.append((text, attachments))

    async def send(
        chat_id: int | None,
        text: str,
        buttons: object | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((chat_id, text, user_id))

    async def acknowledge(**kwargs: object) -> None:
        raise AssertionError("The message edit acknowledges this callback.")

    monkeypatch.setattr(bot_main.mvp, "user_by_max_id", find_user)
    monkeypatch.setattr(bot_main.crud, "decide_parent_student_link", resolve_link)
    monkeypatch.setattr(bot_main, "_send", send)
    event = SimpleNamespace(
        callback=SimpleNamespace(
            callback_id="callback-id",
            payload=payload,
            user=SimpleNamespace(user_id=700),
        ),
        message=SimpleNamespace(
            recipient=SimpleNamespace(chat_id=10),
            body=object(),
        ),
        bot=SimpleNamespace(send_callback=acknowledge),
        edit=edit_message,
    )

    asyncio.run(bot_main._handle_callback(event))

    assert edited == [(expected_student_text, [])]
    assert sent == [
        (
            None,
            "🎉 Ученик Ученик подтвердил синхронизацию! Теперь вы можете просматривать его учебную сводку."
            if approve
            else "К сожалению, запрос на синхронизацию с Ученик был отклонен.",
            800,
        )
    ]
