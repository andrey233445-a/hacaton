from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import CLASS_PASSWORD_KEY
from app.db.database import async_session
from app.db.models import (
    Announcement,
    ChatMessage,
    ClassMember,
    Deadline,
    Homework,
    HomeworkCompletion,
    LinkStatus,
    MoodEntry,
    ParentStudentLink,
    ReminderDelivery,
    Reward,
    RequestStatus,
    ScheduleItem,
    SchoolClass,
    StressSignal,
    User,
    UserRole,
)


def _platform_user_id(value: str | int) -> int:
    try:
        user_id = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("MAX user ID must be numeric") from error
    if user_id <= 0:
        raise ValueError("MAX user ID must be positive")
    return user_id


@dataclass(frozen=True)
class LinkDecision:
    status: str
    parent_name: str
    parent_max_user_id: int
    student_name: str


def hash_class_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived_key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${derived_key.hex()}"


def verify_class_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def _class_password_cipher() -> Fernet:
    if not CLASS_PASSWORD_KEY:
        raise RuntimeError("CLASS_PASSWORD_KEY must be configured to manage class passwords.")
    try:
        return Fernet(CLASS_PASSWORD_KEY.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("CLASS_PASSWORD_KEY is not a valid Fernet key.") from error


def encrypt_class_password(password: str) -> str:
    return _class_password_cipher().encrypt(password.encode()).decode("ascii")


def decrypt_class_password(encrypted: str) -> str:
    try:
        return _class_password_cipher().decrypt(encrypted.encode("ascii")).decode()
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "Cannot decrypt a class password; verify that CLASS_PASSWORD_KEY has not changed."
        ) from error


async def get_user_by_max_id(max_user_id: str) -> User | None:
    async with async_session() as session:
        return await session.scalar(
            select(User).where(User.max_user_id == _platform_user_id(max_user_id))
        )


async def register_user(
    max_user_id: str,
    full_name: str,
    role: UserRole,
    school_name: str | None = None,
) -> User:
    max_user_id_value = _platform_user_id(max_user_id)
    # Администратора в продукте нет: любая регистрация активна сразу.
    status = RequestStatus.APPROVED
    async with async_session() as session:
        existing = await session.scalar(
            select(User).where(User.max_user_id == max_user_id_value)
        )
        if existing is not None:
            if existing.role != UserRole.UNSELECTED:
                raise ValueError("Пользователь уже зарегистрирован; смена роли запрещена.")
            existing.name = full_name
            existing.role = role
            existing.status = status
            existing.school = school_name
            user = existing
        else:
            user = User(
                max_user_id=max_user_id_value,
                name=full_name,
                role=role,
                status=status,
                school=school_name,
            )
            session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def save_unselected_user(max_user_id: str, full_name: str) -> User:
    max_user_id_value = _platform_user_id(max_user_id)
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == max_user_id_value)
        )
        if user is not None:
            if user.role != UserRole.UNSELECTED:
                return user
            user.name = full_name
        else:
            user = User(
                max_user_id=max_user_id_value,
                name=full_name,
                role=UserRole.UNSELECTED,
                status=RequestStatus.APPROVED,
            )
            session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def set_student_grade(user_id: int, grade: int) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is not None and user.role == UserRole.STUDENT:
            user.grade = grade
            await session.commit()


async def list_accounts(
    search: str = "",
    role_filter: str = "",
    status_filter: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[User], int]:
    filters = []
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                User.full_name.ilike(pattern),
                User.school_name.ilike(pattern),
                User.max_user_id.cast(String).ilike(pattern),
            )
        )
    if role_filter:
        filters.append(User.role == UserRole(role_filter))
    if status_filter:
        filters.append(User.status == RequestStatus(status_filter))

    async with async_session() as session:
        total = await session.scalar(
            select(func.count(User.id)).where(*filters)
        )
        rows = await session.scalars(
            select(User)
            .where(*filters)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)


async def change_account_role(user_id: int, role: UserRole) -> User | None:
    if role not in {
        UserRole.STUDENT,
        UserRole.TEACHER,
        UserRole.PARENT,
        UserRole.PSYCHOLOGIST,
    }:
        raise ValueError("Недопустимая роль.")
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        if role != UserRole.TEACHER:
            owned_class_ids = select(SchoolClass.id).where(
                SchoolClass.teacher_id == user_id
            )
            await session.execute(
                delete(ClassMember).where(ClassMember.class_id.in_(owned_class_ids))
            )
            await session.execute(
                delete(SchoolClass).where(SchoolClass.teacher_id == user_id)
            )
        if role != UserRole.STUDENT:
            await session.execute(
                delete(ClassMember).where(ClassMember.student_id == user_id)
            )
            await session.execute(
                delete(ParentStudentLink).where(
                    ParentStudentLink.student_id == user_id
                )
            )
        if role != UserRole.PARENT:
            await session.execute(
                delete(ParentStudentLink).where(
                    ParentStudentLink.parent_id == user_id
                )
            )
        user.role = role
        user.status = RequestStatus.APPROVED
        await session.commit()
        await session.refresh(user)
        return user


async def delete_account(user_id: int) -> bool:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False

        owned_class_ids = select(SchoolClass.id).where(
            SchoolClass.teacher_id == user_id
        )
        owned_homework_ids = select(Homework.id).where(
            or_(Homework.user_id == user_id, Homework.created_by == user_id)
        )

        await session.execute(
            delete(HomeworkCompletion).where(
                or_(
                    HomeworkCompletion.student_id == user_id,
                    HomeworkCompletion.homework_id.in_(owned_homework_ids),
                )
            )
        )
        await session.execute(
            delete(ClassMember).where(
                or_(
                    ClassMember.student_id == user_id,
                    ClassMember.class_id.in_(owned_class_ids),
                )
            )
        )
        await session.execute(
            delete(ParentStudentLink).where(
                or_(
                    ParentStudentLink.parent_id == user_id,
                    ParentStudentLink.student_id == user_id,
                )
            )
        )
        await session.execute(
            update(StressSignal)
            .where(StressSignal.psychologist_id == user_id)
            .values(psychologist_id=None)
        )

        for model, condition in (
            (Announcement, Announcement.teacher_id == user_id),
            (ChatMessage, ChatMessage.user_id == user_id),
            (Deadline, or_(Deadline.user_id == user_id, Deadline.created_by == user_id)),
            (Homework, or_(Homework.user_id == user_id, Homework.created_by == user_id)),
            (MoodEntry, MoodEntry.user_id == user_id),
            (ReminderDelivery, ReminderDelivery.user_id == user_id),
            (Reward, Reward.user_id == user_id),
            (ScheduleItem, or_(ScheduleItem.user_id == user_id, ScheduleItem.created_by == user_id)),
            (StressSignal, StressSignal.student_id == user_id),
            (SchoolClass, SchoolClass.teacher_id == user_id),
        ):
            await session.execute(delete(model).where(condition))

        await session.delete(user)
        await session.commit()
        return True


async def create_school_class(
    teacher_id: int, name: str, login: str, password: str
) -> SchoolClass:
    normalized_login = login.strip().lower()
    if not name.strip() or len(name) > 64:
        raise ValueError("Название класса должно содержать от 1 до 64 символов.")
    if not re.fullmatch(r"[a-z0-9_-]{3,64}", normalized_login):
        raise ValueError("Некорректный логин класса.")
    if len(password) < 8 or len(password.encode("utf-8")) > 256:
        raise ValueError("Пароль должен содержать от 8 до 256 байт.")
    async with async_session() as session:
        teacher = await session.get(User, teacher_id)
        if (
            teacher is None
            or teacher.role != UserRole.TEACHER
            or teacher.status != RequestStatus.APPROVED
        ):
            raise PermissionError("Создавать классы могут только учителя.")
        if await session.scalar(
            select(SchoolClass.id).where(
                func.lower(SchoolClass.login) == normalized_login
            )
        ):
            raise ValueError("Этот логин уже занят. Придумайте другой.")
        school_class = SchoolClass(
            teacher_id=teacher_id,
            name=name.strip(),
            login=normalized_login,
            password_hash=hash_class_password(password),
            password_encrypted=encrypt_class_password(password),
        )
        existing_class_names = [
            item.strip()
            for item in (teacher.classes or "").split(",")
            if item.strip()
        ]
        if name.strip() not in existing_class_names:
            existing_class_names.append(name.strip())
        teacher.classes = ",".join(existing_class_names)
        session.add(school_class)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ValueError("Этот логин уже занят. Придумайте другой.") from None
        await session.refresh(school_class)
        return school_class


async def classes_for_teacher(teacher_id: int) -> list[SchoolClass]:
    async with async_session() as session:
        rows = await session.scalars(
            select(SchoolClass)
            .where(SchoolClass.teacher_id == teacher_id)
            .order_by(SchoolClass.name)
        )
        return list(rows)


async def join_school_class(student_id: int, login: str, password: str) -> str:
    async with async_session() as session:
        student = await session.get(User, student_id)
        if (
            student is None
            or student.role != UserRole.STUDENT
            or student.status != RequestStatus.APPROVED
        ):
            raise PermissionError("Вступать в класс могут только ученики.")
        school_class = await session.scalar(
            select(SchoolClass)
            .options(selectinload(SchoolClass.teacher))
            .where(func.lower(SchoolClass.login) == login.lower())
        )
        if school_class is None or not verify_class_password(
            password, school_class.password_hash
        ):
            raise ValueError("Неверный логин или пароль класса.")
        membership = await session.scalar(
            select(ClassMember).where(
                ClassMember.class_id == school_class.id,
                ClassMember.student_id == student_id,
            )
        )
        if membership is None:
            session.add(
                ClassMember(class_id=school_class.id, student_id=student_id)
            )
        student.class_name = school_class.name
        student.school = school_class.teacher.school
        await session.commit()
        return (
            f"Вы успешно присоединились к классу {school_class.name} "
            f"(учитель: {school_class.teacher.name})!"
        )


async def student_candidates(search: str) -> list[User]:
    normalized = search.strip()
    if not normalized:
        return []
    async with async_session() as session:
        if normalized.isdecimal():
            query = select(User).where(
                User.max_user_id == _platform_user_id(normalized),
                User.role == UserRole.STUDENT,
            )
        else:
            query = select(User).where(
                func.lower(User.name) == normalized.casefold(),
                User.role == UserRole.STUDENT,
            )
        return list(await session.scalars(query.order_by(User.name).limit(10)))


async def request_parent_student_link(
    parent_id: int, student_id: int
) -> tuple[ParentStudentLink, bool]:
    if parent_id == student_id:
        raise ValueError("Нельзя привязать собственный аккаунт.")
    async with async_session() as session:
        parent = await session.get(User, parent_id)
        student = await session.get(User, student_id)
        if (
            parent is None
            or parent.role != UserRole.PARENT
            or parent.status != RequestStatus.APPROVED
            or student is None
            or student.role != UserRole.STUDENT
            or student.status != RequestStatus.APPROVED
        ):
            raise PermissionError("Связь можно запросить только между родителем и учеником.")
        link = await session.scalar(
            select(ParentStudentLink).where(
                ParentStudentLink.parent_id == parent_id,
                ParentStudentLink.student_id == student_id,
            )
        )
        if link is not None and link.status == LinkStatus.APPROVED:
            return link, False
        if link is not None and link.status == LinkStatus.PENDING:
            return link, False
        if link is None:
            link = ParentStudentLink(
                parent_id=parent_id,
                student_id=student_id,
                status=LinkStatus.PENDING,
            )
            session.add(link)
        else:
            link.status = LinkStatus.PENDING
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(ParentStudentLink).where(
                    ParentStudentLink.parent_id == parent_id,
                    ParentStudentLink.student_id == student_id,
                )
            )
            if existing is None:
                raise
            return existing, False
        await session.refresh(link)
        return link, True


async def decide_parent_student_link(
    link_id: int, student_max_user_id: str, approve: bool
) -> LinkDecision | None:
    async with async_session() as session:
        link = await session.scalar(
            select(ParentStudentLink)
            .options(
                selectinload(ParentStudentLink.parent),
                selectinload(ParentStudentLink.student),
            )
            .where(ParentStudentLink.id == link_id)
        )
        if (
            link is None
            or link.status != LinkStatus.PENDING
            or link.student.max_user_id != _platform_user_id(student_max_user_id)
            or link.student.role != UserRole.STUDENT
            or link.student.status != RequestStatus.APPROVED
        ):
            return None
        link.status = LinkStatus.APPROVED if approve else LinkStatus.REJECTED
        result = LinkDecision(
            status=link.status,
            parent_name=link.parent.name,
            parent_max_user_id=link.parent.max_user_id,
            student_name=link.student.name,
        )
        await session.commit()
        return result


async def approved_parent_links(parent_id: int) -> list[ParentStudentLink]:
    async with async_session() as session:
        rows = await session.scalars(
            select(ParentStudentLink)
            .options(selectinload(ParentStudentLink.student))
            .where(
                ParentStudentLink.parent_id == parent_id,
                ParentStudentLink.status == LinkStatus.APPROVED,
            )
            .order_by(ParentStudentLink.created_at)
        )
        return list(rows)
