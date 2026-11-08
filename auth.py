"""Подписанные токены доступа для мини веб-приложения.

Бот выдаёт пользователю ссылку /mini?token=..., токен подписан HMAC-SHA256
на ключе BOT_TOKEN и содержит MAX ID пользователя и срок действия.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from app.config import BOT_TOKEN

TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 дней


def _secret() -> bytes:
    return (BOT_TOKEN or "mini-app-dev-secret").encode()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def make_token(max_user_id: int | str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    exp = int(time.time()) + ttl
    payload = f"{int(max_user_id)}:{exp}"
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return f"{_b64encode(payload.encode())}.{_b64encode(signature)}"


def read_token(token: str | None) -> int | None:
    """Вернуть MAX ID из токена или None, если токен недействителен."""
    if not token or "." not in token:
        return None
    payload_part, signature_part = token.rsplit(".", 1)
    try:
        payload = _b64decode(payload_part).decode()
        signature = _b64decode(signature_part)
    except Exception:  # noqa: BLE001
        return None
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        max_id, exp = payload.split(":")
        if int(exp) < int(time.time()):
            return None
        return int(max_id)
    except ValueError:
        return None
