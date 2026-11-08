from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import DATABASE_URL
from app.db.models import Base

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)
_schema_initialized = False
_schema_lock = asyncio.Lock()


async def _apply_light_migrations(connection: object) -> None:
    """Добавляет новые колонки в уже существующие таблицы (без alembic).

    Нужен для окружений, где БД создавалась ``create_all``, а alembic не
    запускался: без этого, например, ``homework.points`` отсутствует и любая
    операция с домашкой падает. Все команды идемпотентны.
    """
    dialect = getattr(getattr(connection, "dialect", None), "name", "")
    user_columns = (
        ("mood_survey_hour", "INTEGER"),
        ("notify_7d", "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("notify_3d", "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("notify_1d", "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("ui_mode", "VARCHAR(16) NOT NULL DEFAULT 'chat'"),
    )
    other_columns = (("homework", "points", "INTEGER NOT NULL DEFAULT 10"),)
    if dialect == "postgresql":
        try:
            await connection.execute(
                text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'psychologist'")
            )
        except Exception:
            pass
        for name, definition in user_columns:
            await connection.execute(
                text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {definition}")
            )
        for table, name, definition in other_columns:
            await connection.execute(
                text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}"
                )
            )
    elif dialect == "sqlite":
        existing = {}
        for table in ("users", *(item[0] for item in other_columns)):
            rows = (
                await connection.execute(text(f"PRAGMA table_info({table})"))
            ).fetchall()
            existing[table] = {row[1] for row in rows}
        for name, definition in user_columns:
            if name not in existing["users"]:
                await connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {name} {definition}")
                )
        for table, name, definition in other_columns:
            # Пустой набор колонок означает, что таблицы нет — пропускаем.
            if existing.get(table) and name not in existing[table]:
                await connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                )


async def create_db_and_tables() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    async with _schema_lock:
        if not _schema_initialized:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await _apply_light_migrations(connection)
            _schema_initialized = True


async def close_database() -> None:
    global _schema_initialized
    await engine.dispose()
    _schema_initialized = False
