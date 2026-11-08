"""ИИ-психолог: поддерживающий диалог с учеником через OpenAI-совместимый API.

Провайдер настраивается переменными окружения (см. app/config.py):
AI_PSYCHOLOGIST_BASE_URL, AI_PSYCHOLOGIST_API_KEY, AI_PSYCHOLOGIST_MODEL.
По умолчанию используется прокси https://api.plakcel.xyz/v1 и модель kimi-k3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app import config

logger = logging.getLogger(__name__)

# Служебный маркер: модель просит показать ученику кнопку живого психолога.
HUMAN_MARKER = "[[HUMAN_NEEDED]]"

SYSTEM_PROMPT = (
    "Ты — ИИ-психолог школьного бота «Школьный штаб». Тебе пишет школьник. "
    "Отвечай по-русски, тепло, простыми словами и без канцелярита.\n"
    "Правила:\n"
    "- Поддерживай чувства ученика, помогай назвать их и разобраться, "
    "задавай уточняющие вопросы, не читай лекций.\n"
    "- Не ставь диагнозы, не назначай лекарства и не заменяй живого специалиста; "
    "при признаках кризиса мягко предложи обратиться к взрослому, которому "
    "ученик доверяет, или к школьному психологу.\n"
    "- Ответ держи в пределах 120 слов, обычным текстом без заголовков и списков-простыней.\n"
    "- Не обсуждай темы, не связанные с переживаниями, учёбой и отношениями ученика.\n"
    "- Напоминай, что диалог конфиденциален: его не видят ни учителя, ни родители.\n"
    "- Не упоминай, что ты модель Kimi или продукция других компаний: "
    "ты — ИИ-психолог «Школьного штаба».\n"
    "- Если понимаешь, что ученику нужен живой специалист (затяжное подавленное "
    "состояние, мысли о самоповреждении, панические атаки, расстройства сна или "
    "питания, насилие в семье), закончи ответ служебной строкой "
    f"{HUMAN_MARKER} — бот покажет ученику кнопку связи с настоящим "
    "психологом. Ученику не объясняй значение этой строки."
)

FALLBACK_REPLY = (
    "Я рядом и читаю тебя внимательно. Сейчас ИИ-психолог временно недоступен, "
    "но твои чувства всё равно важны. Опиши, что тревожит сильнее всего, — "
    "а если совсем тяжело, нажми «Поговорить с психологом»: с твоего согласия "
    "я передам сигнал школьному психологу."
)

HISTORY_LIMIT = 12
REQUEST_TIMEOUT = 45.0


@dataclass
class ChatSession:
    """История диалога одного ученика с ИИ-психологом."""

    messages: list[dict[str, str]] = field(default_factory=list)

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > HISTORY_LIMIT:
            self.messages = self.messages[-HISTORY_LIMIT:]


_sessions: dict[str, ChatSession] = {}


def get_session(key: object) -> ChatSession:
    return _sessions.setdefault(str(key), ChatSession())


def reset_session(key: object) -> None:
    _sessions.pop(str(key), None)


def is_configured() -> bool:
    return bool(config.AI_PSYCHOLOGIST_API_KEY.strip())


def pop_human_marker(reply: str) -> tuple[str, bool]:
    """Убрать служебный маркер из ответа и сообщить, был ли он там."""
    if HUMAN_MARKER in reply:
        return reply.replace(HUMAN_MARKER, "").strip(), True
    return reply, False


async def _call_api(messages: list[dict[str, str]]) -> str | None:
    """Запрос к OpenAI-совместимому эндпоинту chat/completions.

    Возвращает текст ответа модели или None при любой ошибке сети/API,
    чтобы бот никогда не падал из-за внешнего сервиса.
    """
    url = f"{config.AI_PSYCHOLOGIST_BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": config.AI_PSYCHOLOGIST_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 600,
    }
    headers = {"Authorization": f"Bearer {config.AI_PSYCHOLOGIST_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - внешний сервис не должен ронять бота
        logger.warning("Запрос к ИИ-психологу не удался: %s", exc)
        return None
    text = (content or "").strip()
    return text or None


async def ask(message: str, key: object) -> str:
    """Передать сообщение ученика модели и вернуть ответ с учётом истории."""
    session = get_session(key)
    session.add("user", message)
    if not is_configured():
        logger.info("ИИ-психолог не настроен: не задан AI_PSYCHOLOGIST_API_KEY.")
        return FALLBACK_REPLY
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *session.messages]
    reply = await _call_api(messages)
    if reply is None:
        return FALLBACK_REPLY
    session.add("assistant", reply)
    return reply
