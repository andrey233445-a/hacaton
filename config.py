from __future__ import annotations

import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
CLASS_PASSWORD_KEY = os.getenv("CLASS_PASSWORD_KEY")

# AI-психолог: OpenAI-совместимый API (Kimi K3)
AI_PSYCHOLOGIST_BASE_URL = os.getenv(
    "AI_PSYCHOLOGIST_BASE_URL", "https://api.plakcel.xyz/v1"
)
AI_PSYCHOLOGIST_API_KEY = os.getenv("AI_PSYCHOLOGIST_API_KEY", "")
AI_PSYCHOLOGIST_MODEL = os.getenv("AI_PSYCHOLOGIST_MODEL", "kimi-k3")

# Мини веб-приложение: публичный URL веб-процесса (uvicorn app.admin_web).
# На сервере укажите внешний адрес, например https://hq.example.com
MINI_APP_URL = os.getenv("MINI_APP_URL", "http://127.0.0.1:8000")
# Нативная кнопка мини-приложения MAX (тип open_app).
# Username, под которым мини-приложение зарегистрировано на dev.max.ru. Если
# задан — кнопка «Открыть мини-приложение» нативная (open_app внутри MAX).
# Если пуст — кнопка-ссылка на MINI_APP_URL/mini?token=... (работает всегда).
MINI_APP_BOT_NAME = os.getenv("MINI_APP_BOT_NAME", "")

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "hq_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "change_me")
POSTGRES_DB = os.getenv("POSTGRES_DB", "school_hq_db")
# Явный DATABASE_URL имеет приоритет; иначе строим из POSTGRES_* (в Docker
# хост — имя сервиса `db` из docker-compose.yml).
DATABASE_URL = os.getenv("DATABASE_URL") or (
    "postgresql+asyncpg://"
    f"{quote_plus(POSTGRES_USER)}:{quote_plus(POSTGRES_PASSWORD)}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{quote_plus(POSTGRES_DB)}"
)

# Если проект запущен вне Docker и хост db не резолвится, используем localhost
try:
    import socket

    if "@db:" in DATABASE_URL:
        try:
            socket.gethostbyname("db")
        except socket.gaierror:
            DATABASE_URL = DATABASE_URL.replace("@db:", "@localhost:")
except Exception:
    pass

# Скрытая панель разработчиков (невидима обычным пользователям):
# DEV_MAX_IDS — MAX ID команды через запятую/пробел,
# DEV_COMMAND — секретная команда, открывающая панель смены роли.
DEV_MAX_IDS: set[int] = {
    int(part)
    for part in os.getenv("DEV_MAX_IDS", "").replace(",", " ").split()
    if part.lstrip("-").isdigit()
}
DEV_COMMAND = os.getenv("DEV_COMMAND", "/dev")

