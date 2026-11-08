"""Тесты ИИ-психолога: сервис (Kimi K3 через OpenAI-совместимый API) и обвязка бота."""

from __future__ import annotations

import asyncio

import pytest
from maxapi import Bot
from maxapi.enums.chat_type import ChatType
from maxapi.types import Message, MessageCallback, Recipient
from maxapi.types.callback import Callback
from maxapi.types.users import User as MaxUser

from app import main
from app.db.models import User
from app.keyboards.inline import ButtonsPayload
from app.services import ai_psychologist


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


@pytest.fixture(autouse=True)
def _clean_sessions() -> None:
    ai_psychologist._sessions.clear()


def test_ask_sends_system_prompt_and_keeps_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[dict[str, str]]] = []

    async def fake_call(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return "Слышу тебя. Расскажи подробнее."

    monkeypatch.setattr(ai_psychologist, "_call_api", fake_call)
    monkeypatch.setattr(ai_psychologist.config, "AI_PSYCHOLOGIST_API_KEY", "test-key")

    reply = asyncio.run(ai_psychologist.ask("Мне тревожно перед экзаменом", 42))

    assert reply == "Слышу тебя. Расскажи подробнее."
    assert calls[0][0] == {"role": "system", "content": ai_psychologist.SYSTEM_PROMPT}
    assert calls[0][-1] == {"role": "user", "content": "Мне тревожно перед экзаменом"}

    asyncio.run(ai_psychologist.ask("Боюсь, что не сдам", 42))

    assert calls[1][1] == {"role": "user", "content": "Мне тревожно перед экзаменом"}
    assert calls[1][2] == {
        "role": "assistant",
        "content": "Слышу тебя. Расскажи подробнее.",
    }
    assert calls[1][-1] == {"role": "user", "content": "Боюсь, что не сдам"}


def test_ask_falls_back_when_api_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_call(messages: list[dict[str, str]]) -> str:
        raise AssertionError("Без ключа API вызываться не должен")

    monkeypatch.setattr(ai_psychologist, "_call_api", unexpected_call)
    monkeypatch.setattr(ai_psychologist.config, "AI_PSYCHOLOGIST_API_KEY", "")

    reply = asyncio.run(ai_psychologist.ask("Привет", 7))

    assert reply == ai_psychologist.FALLBACK_REPLY
    assert ai_psychologist.is_configured() is False


def test_ask_falls_back_when_api_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_call(messages: list[dict[str, str]]) -> None:
        return None

    monkeypatch.setattr(ai_psychologist, "_call_api", failing_call)
    monkeypatch.setattr(ai_psychologist.config, "AI_PSYCHOLOGIST_API_KEY", "key")

    reply = asyncio.run(ai_psychologist.ask("Привет", 8))

    assert reply == ai_psychologist.FALLBACK_REPLY


def test_student_ai_chat_button_opens_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[str, ButtonsPayload | None]] = []
    bot = Bot(token="test-token")

    async def find_user(max_user_id: str) -> User:
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
    monkeypatch.setattr(main, "_send", send)
    monkeypatch.setattr(bot, "send_callback", ack)

    asyncio.run(main._handle_callback(callback_event("student:ai_chat", bot)))

    assert main._states.get("42", {}).get("step") == "ai_chat"
    assert "ИИ-психолог" in sent[0][0]
    payloads = [button.payload for row in sent[0][1].buttons for button in row]
    assert payloads == ["student:ai_chat_exit"]
    main._states.pop("42", None)


def test_ai_chat_step_routes_message_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[str, ButtonsPayload | None]] = []
    asked: list[tuple[str, int]] = []

    async def fake_ask(message: str, key: object) -> str:
        asked.append((message, int(key)))
        return "Я рядом. Что чувствуешь прямо сейчас?"

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    monkeypatch.setattr(main.ai_psychologist, "ask", fake_ask)
    monkeypatch.setattr(main, "_send", send)

    async def noop_action(chat_id: int | None = None, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.bot, "send_action", noop_action)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="ai_chat",
            text="Волнуюсь перед экзаменом",
            state={},
        )
    )

    assert handled is True
    assert asked == [("Волнуюсь перед экзаменом", 42)]
    assert "Я рядом" in sent[0][0]
    payloads = [button.payload for row in sent[0][1].buttons for button in row]
    assert payloads == ["student:ai_chat_exit"]


def test_ai_chat_step_crisis_message_skips_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[str, ButtonsPayload | None]] = []

    async def unexpected_ask(message: str, key: object) -> str:
        raise AssertionError("В кризисной ветке модель вызываться не должна")

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    monkeypatch.setattr(main.ai_psychologist, "ask", unexpected_ask)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="ai_chat",
            text="Я хочу умереть",
            state={},
        )
    )

    assert handled is True
    assert "8-800-2000-122" in sent[0][0]
    payloads = [button.payload for row in sent[0][1].buttons for button in row]
    assert "stress:yes" in payloads


def test_ai_chat_step_exit_command_closes_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[str] = []
    states: list[str | None] = []

    async def set_state(user: User, step: str | None, **data: object) -> None:
        states.append(step)

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
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="ai_chat",
            text="выйти",
            state={},
        )
    )

    assert handled is True
    assert states == [None]
    assert "Диалог завершён" in sent[0]


def test_ai_chat_step_shows_typing_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    actions: list[int | None] = []

    async def fake_ask(message: str, key: object) -> str:
        return "Слушаю тебя внимательно."

    async def fake_action(chat_id: int | None = None, **kwargs: object) -> None:
        actions.append(chat_id)

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        return None

    monkeypatch.setattr(main.ai_psychologist, "ask", fake_ask)
    monkeypatch.setattr(main.bot, "send_action", fake_action)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="ai_chat",
            text="Привет",
            state={},
        )
    )

    assert handled is True
    assert actions == [10]


def test_ai_chat_step_human_marker_offers_live_psychologist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(max_user_id="42", role="student", status="approved")
    sent: list[tuple[str, ButtonsPayload | None]] = []

    async def fake_ask(message: str, key: object) -> str:
        return "Твои чувства важны. " + ai_psychologist.HUMAN_MARKER

    async def send(
        chat_id: int | None,
        text: str,
        buttons: ButtonsPayload | None = None,
        user_id: int | None = None,
    ) -> None:
        sent.append((text, buttons))

    async def noop_action(chat_id: int | None = None, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(main.ai_psychologist, "ask", fake_ask)
    monkeypatch.setattr(main.bot, "send_action", noop_action)
    monkeypatch.setattr(main, "_send", send)

    handled = asyncio.run(
        main._handle_input_step(
            chat_id=10,
            max_id=42,
            user=user,
            step="ai_chat",
            text="Уже месяц ничего не радует",
            state={},
        )
    )

    assert handled is True
    assert ai_psychologist.HUMAN_MARKER not in sent[0][0]
    assert "живой специалист" in sent[0][0]
    payloads = [button.payload for row in sent[0][1].buttons for button in row]
    assert "stress:yes" in payloads
    assert "stress:live" in payloads
