from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.database import async_session, create_db_and_tables
from app.db.models import RequestStatus, User, UserRole


def _initial_status(role: str) -> RequestStatus:
    """Администратора в продукте нет — регистрация активна сразу для всех ролей."""
    return RequestStatus.APPROVED


async def get_or_create_user(
    max_user_id: str,
    name: str | None = None,
    role: str | None = None,
) -> User:
    await create_db_and_tables()
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )
        if user is None:
            role_value = role or UserRole.UNSELECTED.value
            if role_value not in {item.value for item in UserRole}:
                raise ValueError("Unsupported user role")
            user = User(
                max_user_id=int(max_user_id),
                name=name or "Пользователь",
                role=role_value,
                status=_initial_status(role_value),
                created_at=datetime.now(timezone.utc),
            )
            session.add(user)
        else:
            if name is not None:
                user.name = name
            if role is not None and user.role != role:
                if user.role != UserRole.UNSELECTED:
                    raise ValueError("A registered user's role cannot be changed")
                if role not in {item.value for item in UserRole}:
                    raise ValueError("Unsupported user role")
                user.role = role
                user.status = _initial_status(role)
        await session.commit()
        await session.refresh(user)
        return user


async def update_user_role(max_user_id: str, role: str) -> User | None:
    await create_db_and_tables()
    if role not in {item.value for item in UserRole}:
        raise ValueError("Unsupported user role")
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )
        if user is None:
            return None
        if user.role != role and user.role != UserRole.UNSELECTED:
            return None
        user.role = role
        user.status = _initial_status(role)
        await session.commit()
        await session.refresh(user)
        return user


async def add_points(max_user_id: str, value: int) -> User | None:
    await create_db_and_tables()
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )
        if user is None:
            return None
        user.points += value
        await session.commit()
        await session.refresh(user)
        return user
