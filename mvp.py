from __future__ import annotations

import random
import re
from datetime import date, datetime, time, timedelta

from sqlalchemy import Date, and_, cast, delete, func, or_, select
from sqlalchemy.orm import selectinload

from app.db.database import async_session, create_db_and_tables
from app.db.models import (
    Announcement,
    ChatMessage,
    ClassMember,
    Deadline,
    Homework,
    HomeworkCompletion,
    MoodEntry,
    LinkStatus,
    ParentStudentLink,
    ReminderDelivery,
    RequestStatus,
    Reward,
    ScheduleItem,
    SchoolClass,
    StressSignal,
    User,
)

STRESS_TERMS: dict[str, tuple[str, ...]] = {
    "усталость": ("устал", "не высыпаюсь", "нет сил", "выгорел"),
    "тревога": ("страшно", "тревож", "паника", "боюсь"),
    "конфликты": ("ненавижу", "ругаемся", "ссор", "буллинг"),
    "одиночество": ("одиноко", "никому не нужен", "нет друзей"),
    "безнадёжность": ("не вижу смысла", "не могу больше", "всё бессмысленно"),
    "раздражение": ("всё бесит", "достало", "раздраж"),
}

# Каталог наград: ключ -> (название, стоимость в очков, описание).
#
# Оставлены только «крутые» позиции: редкие титулы, оформление профиля
# и игровые усиления. Мелкие безымянные значки («Клевер», «Ниндзя», «Ночная
# сова») убраны — они не давали ощутимой ценности и засоряли каталог.
REWARD_CATALOG: dict[str, tuple[str, int, str]] = {
    "title_captain": (
        "⚓ Титул «Капитан класса»",
        120,
        "Самый редкий титул — лидер, за которым идут.",
    ),
    "title_hero": (
        "🦸 Титул «Герой класса»",
        90,
        "Значок для того, кто помогает одноклассникам.",
    ),
    "title_champion": (
        "🏆 Титул «Чемпион недели»",
        50,
        "Почётный значок рядом с именем в профиле.",
    ),
    "title_genius": (
        "🧠 Титул «Гений домашки»",
        80,
        "Значок для тех, кто сдаёт задания раньше срока.",
    ),
    "frame_gold": (
        "🖼 Золотая рамка профиля",
        80,
        "Редкая золотая рамка — видно всему классу.",
    ),
    "emoji_cosmos": (
        "🚀 Стиль профиля «Космос»",
        50,
        "Космическое оформление профиля.",
    ),
    "points_boost_3": (
        "🚀 Мегабуст очков ×3",
        160,
        "Утраивает баллы за каждую сданную домашку. Работает вместо буста ×2.",
    ),
    "points_boost": (
        "⚡ Буст очков ×2",
        90,
        "Удваивает баллы за каждую сданную домашку, пока открыт.",
    ),
    "daily_spin": (
        "🎰 Колесо удачи",
        100,
        "Раз в день можно крутить колесо и получать до 50 очков бесплатно.",
    ),
    "theme_sunset": (
        "🌇 Тема «Закат»",
        120,
        "Тёплое закатное оформление мини-приложения. Открывается только за очки.",
    ),
    "card_pack": (
        "🃏 Пак редких антистресс-карточек",
        45,
        "Открывает редкие карточки в разделе «🧘 Антистресс».",
    ),
}

# Тёмная и светлая темы мини-приложения доступны всем сразу; «Закат» —
# премиальная и открывается только наградой ``theme_sunset``.
THEME_DEFAULT = "dark"
FREE_THEMES: tuple[str, ...] = ("dark", "light")
REWARD_THEMES: dict[str, str] = {"sunset": "theme_sunset"}


def theme_is_unlocked(theme: str, reward_keys: set[str]) -> bool:
    """Можно ли пользователю включить эту тему оформления."""
    if theme in FREE_THEMES:
        return True
    reward = REWARD_THEMES.get(theme)
    return reward is not None and reward in reward_keys

# Очки за ежедневный опрос настроения.
MOOD_POINTS = 3

# Призы колеса удачи (награда «🎰 Колесо удачи»): один спин в сутки.
SPIN_PRIZES: tuple[int, ...] = (0, 5, 10, 15, 20, 30, 50)

def normalize_school(value: object) -> str:
    """Приводит название школы к сравнимому виду.

    Название школы ученик и психолог вводят вручную, поэтому сравнивать строки
    «как есть» нельзя: «Школа №1», «школа N1», «Школа  1» и «МБОУ Школа №1» —
    одна и та же школа. Убираем регистр, пунктуацию, лишние пробелы, знак «№»
    и буквенный префикс номера, разделяем буквы и цифры.
    """
    text = str(value or "").strip().casefold().replace("ё", "е").replace("\u00a0", " ")
    text = text.replace("№", " ")
    # «n1» / «н 1» → просто номер: буквенный префикс перед числом ничего не значит.
    text = re.sub(r"\b[nн]\s*(?=\d)", " ", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    # «школа1» и «школа 1» должны совпадать.
    text = re.sub(r"(?<=[a-zа-я])(?=\d)|(?<=\d)(?=[a-zа-я])", " ", text)
    return " ".join(text.split())




async def ensure_database() -> None:
    await create_db_and_tables()


async def user_by_max_id(max_user_id: str) -> User | None:
    await ensure_database()
    async with async_session() as session:
        return await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )


async def update_profile(max_user_id: str, **values: object) -> User:
    await ensure_database()
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )
        if user is None:
            user = User(max_user_id=int(max_user_id), role="unselected", name="Пользователь")
            session.add(user)
        original_role = getattr(user.role, "value", user.role)
        requested_role = values.get("role")
        requested_role = getattr(requested_role, "value", requested_role)
        role_transition = (
            original_role == "unselected"
            and requested_role is not None
            and requested_role != "unselected"
        )
        expected_status = None
        if role_transition:
            # Администратора нет: любая роль активируется сразу.
            expected_status = "approved"
            requested_status = values.get("status")
            requested_status = getattr(requested_status, "value", requested_status)
            if requested_status is not None and requested_status != expected_status:
                raise ValueError("The initial status is determined by the selected role")

        for key, value in values.items():
            if not hasattr(user, key):
                raise ValueError(f"Unknown profile field: {key}")
            if (
                key == "role"
                and original_role != "unselected"
                and original_role != getattr(value, "value", value)
            ):
                raise ValueError("A registered user's role cannot be changed")
            if key == "status":
                status_value = getattr(value, "value", value)
                current_status = getattr(user.status, "value", user.status)
                if role_transition:
                    if status_value != expected_status:
                        raise ValueError(
                            "The initial status is determined by the selected role"
                        )
                elif status_value != current_status:
                    raise ValueError("Only an administrator can change user status")
                continue
            setattr(user, key, value)
        if role_transition:
            assert expected_status is not None
            user.status = expected_status
        await session.commit()
        await session.refresh(user)
        return user


async def dev_force_profile(max_user_id: str, **values: object) -> User:
    """Панель разработчиков: безусловное обновление полей профиля.

    Обходит защиту «роль изменить нельзя» и проверку статуса — используется
    только скрытой панелью в main.py для ID команды (config.DEV_MAX_IDS).
    """
    await ensure_database()
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )
        if user is None:
            raise ValueError("Profile not found")
        for key, value in values.items():
            if not hasattr(user, key):
                raise ValueError(f"Unknown profile field: {key}")
            setattr(user, key, value)
        await session.commit()
        await session.refresh(user)
        return user


# Ученик для демонстрации: один аккаунт разработчика не может быть одновременно
# учеником и психологом (один MAX ID = одна роль), поэтому для показа кабинета
# психолога используется отдельная служебная запись.
DEMO_STUDENT_MAX_ID = 900001
DEMO_STUDENT_NAME = "Тестовый ученик (демо)"


async def ensure_demo_student(
    school_name: str | None = None, class_name: str | None = None
) -> User:
    """Создать или обновить служебного ученика для демонстрации сигналов."""
    await ensure_database()
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == DEMO_STUDENT_MAX_ID)
        )
        if user is None:
            user = User(
                max_user_id=DEMO_STUDENT_MAX_ID,
                full_name=DEMO_STUDENT_NAME,
                role="student",
                status="approved",
                school_name=school_name,
                class_name=class_name or "9А",
            )
            session.add(user)
        else:
            if school_name:
                user.school_name = school_name
            if class_name:
                user.class_name = class_name
        await session.commit()
        await session.refresh(user)
        return user


async def approve_all_pending() -> int:
    """Приводит старые заявки к активному статусу.

    Администратора в продукте нет, поэтому «ожидающих» аккаунтов быть не должно:
    при старте бота все старые строки со статусом pending/rejected становятся
    активными. Возвращает число обновлённых аккаунтов.
    """
    await ensure_database()
    async with async_session() as session:
        rows = list(
            await session.scalars(
                select(User).where(User.status != RequestStatus.APPROVED)
            )
        )
        for row in rows:
            row.status = RequestStatus.APPROVED
        if rows:
            await session.commit()
        return len(rows)


async def approve_user(max_user_id: str) -> User | None:
    """Активировать аккаунт (страховка для строк, созданных старой версией)."""
    await ensure_database()
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.max_user_id == int(max_user_id))
        )
        if user is None:
            return None
        if user.status != RequestStatus.APPROVED:
            user.status = RequestStatus.APPROVED
            await session.commit()
            await session.refresh(user)
        return user


async def list_directory(limit: int = 40) -> list[User]:
    """Список аккаунтов системы — для скрытой панели разработчиков.

    Помогает сразу увидеть, почему сигнал не доходит: нет ученика, психолог не
    подтверждён или школы не совпадают.
    """
    await ensure_database()
    async with async_session() as session:
        return list(
            await session.scalars(select(User).order_by(User.id).limit(limit))
        )



async def first_school_class() -> SchoolClass | None:
    """Первый класс в БД — для автоподстановки класса в dev-панели."""
    await ensure_database()
    async with async_session() as session:
        return await session.scalar(
            select(SchoolClass).order_by(SchoolClass.id).limit(1)
        )


async def user_by_id(user_id: int) -> User | None:
    await ensure_database()
    async with async_session() as session:
        return await session.get(User, user_id)


async def link_parent_to_child(parent: User, child_max_id: str) -> bool:
    from app.db.crud import request_parent_student_link, student_candidates

    candidates = await student_candidates(child_max_id)
    if len(candidates) != 1:
        return False
    _, created = await request_parent_student_link(parent.id, candidates[0].id)
    return created


async def student_announcements(student: User) -> list[Announcement]:
    await ensure_database()
    if not student.class_name:
        return []
    async with async_session() as session:
        # Класс сравниваем без учёта регистра; автор — учитель той же школы
        # (NULL == '') ИЛИ классный руководитель класса ученика.
        rows = await session.scalars(
            select(Announcement)
            .where(
                func.lower(Announcement.class_name) == student.class_name.lower(),
                _class_creator_condition(student, Announcement.teacher_id),
            )
            .order_by(Announcement.created_at.desc())
            .limit(10)
        )
        return list(rows)


async def create_schedule_item(
    owner_id: int,
    creator_id: int,
    day_of_week: int,
    lesson_time: time,
    subject: str,
    room: str | None = None,
    class_name: str | None = None,
) -> ScheduleItem:
    await ensure_database()
    async with async_session() as session:
        item = ScheduleItem(
            user_id=owner_id,
            created_by=creator_id,
            day_of_week=day_of_week,
            time=lesson_time,
            subject=subject,
            room=room,
            class_name=class_name,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


def _class_lessons_condition(class_name: str, school: str | None):
    """Уроки класса, добавленные учителями той же школы."""
    conditions = [func.lower(ScheduleItem.class_name) == (class_name or "").strip().lower()]
    if school:
        conditions.append(
            ScheduleItem.created_by.in_(
                select(User.id).where(
                    User.role == "teacher",
                    func.lower(User.school) == (school or "").strip().lower(),
                )
            )
        )
    return and_(*conditions)


async def schedule_for_day(user: User, weekday: int) -> list[ScheduleItem]:
    # уроки своего класса и не может добавлять или удалять их.
    await ensure_database()
    async with async_session() as session:
        if user.role == "student":
            if not user.class_name:
                return []
            rows = await session.scalars(
                select(ScheduleItem)
                .where(
                    ScheduleItem.day_of_week == weekday,
                    _class_lessons_condition(user.class_name, user.school),
                )
                .order_by(ScheduleItem.time)
            )
            return list(rows)
        rows = await session.scalars(
            select(ScheduleItem)
            .where(
                ScheduleItem.day_of_week == weekday,
                ScheduleItem.user_id == user.id,
            )
            .order_by(ScheduleItem.time)
        )
        return list(rows)


async def schedule_for_class(
    class_name: str, school: str | None, weekday: int | None = None
) -> list[ScheduleItem]:
    """Расписание класса для учителя: все уроки, добавленные учителями школы."""
    await ensure_database()
    async with async_session() as session:
        statement = select(ScheduleItem).where(
            _class_lessons_condition(class_name, school)
        )
        if weekday is not None:
            statement = statement.where(ScheduleItem.day_of_week == weekday)
        rows = await session.scalars(
            statement.order_by(ScheduleItem.day_of_week, ScheduleItem.time)
        )
        return list(rows)


async def delete_schedule_item(user_id: int, item_id: int) -> bool:
    await ensure_database()
    async with async_session() as session:
        item = await session.get(ScheduleItem, item_id)
        if item is None or item.user_id != user_id or item.created_by != user_id:
            return False
        await session.delete(item)
        await session.commit()
        return True


async def update_schedule_item(
    teacher_id: int,
    item_id: int,
    *,
    day_of_week: int | None = None,
    lesson_time: time | None = None,
    subject: str | None = None,
    room: str | None = None,
) -> ScheduleItem | None:
    """Изменить урок расписания. Править может только учитель-создатель."""
    await ensure_database()
    async with async_session() as session:
        item = await session.get(ScheduleItem, item_id)
        if item is None or item.created_by != teacher_id:
            return None
        if day_of_week is not None:
            item.day_of_week = day_of_week
        if lesson_time is not None:
            item.time = lesson_time
        if subject is not None:
            item.subject = subject
        if room is not None:
            item.room = room
        await session.commit()
        await session.refresh(item)
        return item


async def create_homework(
    owner_id: int,
    creator_id: int,
    subject: str,
    description: str,
    deadline: datetime,
    class_name: str | None = None,
    points: int = 10,
) -> Homework:
    await ensure_database()
    async with async_session() as session:
        item = Homework(
            user_id=owner_id,
            created_by=creator_id,
            subject=subject,
            description=description,
            deadline=deadline,
            class_name=class_name,
            points=max(0, min(100, int(points))),
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


def _class_creator_condition(user: User, column):
    """Условие на автора классного задания.

    Автор годится, если он:
    - учитель той же школы (школа нормализуется: NULL == ''), ИЛИ
    - классный руководитель класса, в котором ученик состоит (ClassMember).
    Второй путь спасает, когда класс создал пользователь без роли teacher
    или названия школ у учителя и ученика различаются.
    """
    return or_(
        column.in_(
            select(User.id).where(
                User.role == "teacher",
                func.coalesce(User.school, "") == (user.school or ""),
            )
        ),
        column.in_(
            select(SchoolClass.teacher_id)
            .join(ClassMember, ClassMember.class_id == SchoolClass.id)
            .where(
                ClassMember.student_id == user.id,
                func.lower(SchoolClass.name) == (user.class_name or "").lower(),
            )
        ),
    )


async def active_homework(user: User) -> list[tuple[Homework, bool]]:
    await ensure_database()
    async with async_session() as session:
        # Свои активные записи видны всем независимо от роли: учитель может
        # проверять раздел со своего аккаунта, а ученик — видеть то, что сам добавил.
        conditions = [
            and_(Homework.user_id == user.id, Homework.status == "active")
        ]
        # Классные задания: название класса сравниваем без учёта регистра,
        # школа может быть пустой строкой/NULL — автор проверяется мягко.
        if user.class_name:
            conditions.append(
                and_(
                    func.lower(Homework.class_name) == user.class_name.lower(),
                    Homework.created_by != user.id,
                    Homework.status == "active",
                    _class_creator_condition(user, Homework.created_by),
                )
            )
        items = list(
            await session.scalars(
                select(Homework).where(or_(*conditions)).order_by(Homework.deadline)
            )
        )
        completions = set(
            await session.scalars(
                select(HomeworkCompletion.homework_id).where(
                    HomeworkCompletion.student_id == user.id,
                    HomeworkCompletion.homework_id.in_([item.id for item in items] or [-1]),
                )
            )
        )
        return [(item, item.id in completions) for item in items]


async def complete_homework(user: User, homework_id: int) -> bool:
    await ensure_database()
    async with async_session() as session:
        item = await session.get(Homework, homework_id)
        if item is None or item.status != "active":
            return False
        if item.user_id != user.id and (
            not user.class_name
            or (item.class_name or "").lower() != (user.class_name or "").lower()
            or item.created_by == user.id
        ):
            return False
        if item.user_id != user.id:
            creator = await session.get(User, item.created_by)
            same_school_teacher = (
                creator is not None
                and getattr(creator.role, "value", str(creator.role)) == "teacher"
                and (creator.school or "") == (user.school or "")
            )
            class_teacher_row = await session.scalar(
                select(SchoolClass.id)
                .join(ClassMember, ClassMember.class_id == SchoolClass.id)
                .where(
                    SchoolClass.teacher_id == item.created_by,
                    ClassMember.student_id == user.id,
                    func.lower(SchoolClass.name) == (user.class_name or "").lower(),
                )
            )
            if not same_school_teacher and class_teacher_row is None:
                return False
        completed = await session.scalar(
            select(HomeworkCompletion.id).where(
                HomeworkCompletion.homework_id == item.id,
                HomeworkCompletion.student_id == user.id,
            )
        )
        if completed is not None:
            return False
        if item.user_id == user.id:
            item.status = "done"
            item.completed_at = datetime.now()
        else:
            session.add(HomeworkCompletion(homework_id=item.id, student_id=user.id))
        student_row = await session.get(User, user.id)
        if student_row is None:
            return False
        student_row.points += await _homework_gain(session, student_row.id, item)
        await session.commit()
        return True


async def assignment_points(teacher: User, homework_id: int) -> int | None:
    """Стоимость задания в баллах. None — если задания нет или учитель не автор."""
    await ensure_database()
    async with async_session() as session:
        item = await session.get(Homework, homework_id)
        if item is None or item.created_by != teacher.id:
            return None
        return int(getattr(item, "points", 0) or 0)


async def teacher_assignments(teacher: User) -> list[Homework]:
    """Активные классные задания, созданные учителем."""
    await ensure_database()
    async with async_session() as session:
        rows = await session.scalars(
            select(Homework)
            .where(Homework.created_by == teacher.id, Homework.status == "active")
            .order_by(Homework.deadline)
        )
        return list(rows)


async def homework_roster(
    teacher: User, homework_id: int
) -> list[tuple[User, bool]] | None:
    """Ученики класса задания с отметками сдачи. None — если учитель не автор."""
    await ensure_database()
    async with async_session() as session:
        item = await session.get(Homework, homework_id)
        if item is None or item.created_by != teacher.id:
            return None
        students = list(
            await session.scalars(
                select(User)
                .where(
                    User.role == "student",
                    func.lower(User.class_name) == (item.class_name or "").lower(),
                )
                .order_by(User.name)
            )
        )
        done_ids = set(
            await session.scalars(
                select(HomeworkCompletion.student_id).where(
                    HomeworkCompletion.homework_id == item.id,
                    HomeworkCompletion.student_id.in_(
                        [student.id for student in students] or [-1]
                    ),
                )
            )
        )
        return [(student, student.id in done_ids) for student in students]


async def set_homework_completion(
    teacher: User, homework_id: int, student_id: int, done: bool
) -> bool:
    """Отметить/снять сдачу задания ученику. Право есть только у автора-учителя."""
    await ensure_database()
    async with async_session() as session:
        item = await session.get(Homework, homework_id)
        if item is None or item.created_by != teacher.id or item.status != "active":
            return False
        student = await session.get(User, student_id)
        if student is None or getattr(student.role, "value", str(student.role)) != "student":
            return False
        if (student.class_name or "").strip().lower() != (item.class_name or "").strip().lower():
            return False
        existing = await session.scalar(
            select(HomeworkCompletion.id).where(
                HomeworkCompletion.homework_id == item.id,
                HomeworkCompletion.student_id == student_id,
            )
        )
        if done and existing is None:
            session.add(HomeworkCompletion(homework_id=item.id, student_id=student_id))
            student.points += await _homework_gain(session, student.id, item)
        elif not done and existing is not None:
            await session.execute(
                delete(HomeworkCompletion).where(HomeworkCompletion.id == existing)
            )
            student.points = max(
                0, student.points - await _homework_gain(session, student.id, item)
            )
        await session.commit()
        return True


async def create_deadline(
    owner_id: int,
    creator_id: int,
    title: str,
    due: datetime,
    kind: str,
    class_name: str | None = None,
) -> Deadline:
    await ensure_database()
    async with async_session() as session:
        item = Deadline(
            user_id=owner_id,
            created_by=creator_id,
            title=title,
            date=due,
            kind=kind,
            class_name=class_name,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def upcoming_deadlines(user: User) -> list[Deadline]:
    await ensure_database()
    now = datetime.now()
    async with async_session() as session:
        # Свои записи видны всем независимо от роли (учитель мог добавить
        # дедлайн со своего аккаунта, ученик — личный дедлайн).
        conditions = [Deadline.user_id == user.id]
        # Классные дедлайны: название класса без учёта регистра, автор —
        # учитель той же школы ИЛИ классный руководитель класса ученика.
        if user.class_name:
            conditions.append(
                and_(
                    func.lower(Deadline.class_name) == user.class_name.lower(),
                    _class_creator_condition(user, Deadline.created_by),
                )
            )
        rows = await session.scalars(
            select(Deadline)
            .where(Deadline.date >= now, or_(*conditions))
            .order_by(Deadline.date)
            .limit(20)
        )
        return list(rows)


async def save_mood(user: User, score: int, comment: str | None = None) -> bool:
    if score not in range(1, 6):
        raise ValueError("Mood score must be between 1 and 5")
    await ensure_database()
    async with async_session() as session:
        exists = await session.scalar(
            select(MoodEntry.id).where(
                MoodEntry.user_id == user.id, MoodEntry.date == date.today()
            )
        )
        if exists is not None:
            return False
        session.add(
            MoodEntry(user_id=user.id, date=date.today(), score=score, comment=comment)
        )
        student_row = await session.get(User, user.id)
        if student_row is None:
            raise ValueError("Student profile no longer exists")
        reward_keys = set(
            await session.scalars(
                select(Reward.item_key).where(Reward.user_id == user.id)
            )
        )
        student_row.points += _mood_points(reward_keys, datetime.now())
        await session.commit()
        return True


def _mood_points(reward_keys: set[str], now: datetime) -> int:
    """Очки за отметку настроения.

    Базовая награда фиксированная: отдельные платные «усиления за настроение»
    убраны, чтобы награды не подменяли саму заботу о себе.
    """
    return MOOD_POINTS


async def mood_gain(user_id: int) -> int:
    """Сколько очков получит пользователь за сегодняшнюю отметку настроения."""
    await ensure_database()
    async with async_session() as session:
        reward_keys = set(
            await session.scalars(select(Reward.item_key).where(Reward.user_id == user_id))
        )
    return _mood_points(reward_keys, datetime.now())


async def mood_history(user_id: int, days: int = 14) -> list[MoodEntry]:
    await ensure_database()
    async with async_session() as session:
        rows = await session.scalars(
            select(MoodEntry)
            .where(
                MoodEntry.user_id == user_id,
                MoodEntry.date >= date.today() - timedelta(days=days - 1),
            )
            .order_by(MoodEntry.date.desc())
        )
        return list(rows)


def analyze_stress(text: str) -> tuple[int, list[str]]:
    normalized = text.casefold()
    themes = [
        theme
        for theme, terms in STRESS_TERMS.items()
        if any(term in normalized for term in terms)
    ]
    if any(term in normalized for term in ("хочу умереть", "покончить с собой", "убить себя")):
        return 5, themes + ["требуется срочная поддержка"]
    if len(themes) >= 2:
        return 4, themes
    if themes:
        return 3, themes
    return 1, []


async def check_stress_threshold(user: User) -> bool:
    await ensure_database()
    today = date.today()
    yesterday = today - timedelta(days=1)
    async with async_session() as session:
        today_exists = (
            await session.scalar(
                select(ChatMessage.id).where(
                    ChatMessage.user_id == user.id,
                    ChatMessage.risk_score >= 4,
                    cast(ChatMessage.created_at, Date) == today,
                )
            )
        ) is not None
        yesterday_exists = (
            await session.scalar(
                select(ChatMessage.id).where(
                    ChatMessage.user_id == user.id,
                    ChatMessage.risk_score >= 4,
                    cast(ChatMessage.created_at, Date) == yesterday,
                )
            )
        ) is not None
        return today_exists and yesterday_exists


async def last_chat_message(user: User) -> ChatMessage | None:
    await ensure_database()
    async with async_session() as session:
        return await session.scalar(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.desc())
        )


SIGNAL_DEFAULT_RISK = 4


async def stress_snapshot(user: User, days: int = 7) -> tuple[int, list[str]]:
    """Риск и темы для сигнала психологу — по свежей переписке, а не «на глаз».

    Раньше сигнал брал риск из последнего сообщения в БД и при пустой истории
    всегда писал ``4``. Теперь объединяем все сообщения за последние ``days``
    дней: берём максимальный риск и все найденные темы. Если ученик вообще
    ничего не писал, но сам просит помощи, риск считаем заметным (4) — факт
    обращения уже значим, но не выдаём его за кризис «5».
    """
    await ensure_database()
    since = datetime.now() - timedelta(days=days)
    async with async_session() as session:
        rows = list(
            await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.user_id == user.id,
                    ChatMessage.created_at >= since,
                )
                .order_by(ChatMessage.created_at.desc())
            )
        )
    if not rows:
        return SIGNAL_DEFAULT_RISK, []
    risk = max(int(row.risk_score or 0) for row in rows)
    themes: list[str] = []
    for row in rows:
        for theme in (row.themes or "").split(", "):
            cleaned = theme.strip()
            if cleaned and cleaned not in themes:
                themes.append(cleaned)
    if risk < 1:
        risk = SIGNAL_DEFAULT_RISK
    return risk, themes


async def record_chat_message(user: User, text: str) -> tuple[int, list[str], bool]:
    score, themes = analyze_stress(text)
    await ensure_database()
    async with async_session() as session:
        session.add(
            ChatMessage(
                user_id=user.id,
                text=text,
                risk_score=score,
                themes=", ".join(themes) if themes else None,
            )
        )
        await session.commit()
        severe_days = list(
            await session.scalars(
                select(cast(ChatMessage.created_at, Date))
                .where(ChatMessage.user_id == user.id, ChatMessage.risk_score >= 4)
                .group_by(cast(ChatMessage.created_at, Date))
                .order_by(cast(ChatMessage.created_at, Date).desc())
                .limit(2)
            )
        )
    threshold = (
        score >= 4
        and len(severe_days) == 2
        and severe_days[0] == date.today()
        and severe_days[1] == date.today() - timedelta(days=1)
    )
    return score, themes, threshold


async def create_announcement(teacher_id: int, class_name: str, message: str) -> None:
    await ensure_database()
    async with async_session() as session:
        announcement = Announcement(
            teacher_id=teacher_id, class_name=class_name, text=message
        )
        session.add(announcement)
        await session.commit()


async def announcement_recipients(teacher_id: int, class_name: str) -> list[User]:
    """Ученики для push-доставки объявления класса.

    Сюда входят участники класса учителя (вступившие по логину и паролю)
    и ученики того же класса и школы, включая случаи пустой школы.
    """
    await ensure_database()
    async with async_session() as session:
        teacher = await session.get(User, teacher_id)
        school = (teacher.school or "") if teacher is not None else ""
        class_key = (class_name or "").lower()
        member_ids = (
            select(ClassMember.student_id)
            .join(SchoolClass, SchoolClass.id == ClassMember.class_id)
            .where(
                SchoolClass.teacher_id == teacher_id,
                func.lower(SchoolClass.name) == class_key,
            )
        )
        rows = await session.scalars(
            select(User).where(
                User.role == "student",
                or_(
                    User.id.in_(member_ids),
                    and_(
                        func.lower(User.class_name) == class_key,
                        func.coalesce(User.school, "") == school,
                    ),
                ),
            )
        )
        return list(rows)


async def class_students(teacher: User) -> list[User]:
    await ensure_database()
    classes = [value.strip() for value in (teacher.classes or "").split(",") if value.strip()]
    if not classes or not teacher.school:
        return []
    async with async_session() as session:
        query = select(User).where(User.role == "student")
        query = query.where(
            func.lower(User.class_name).in_([c.lower() for c in classes]),
            func.lower(User.school) == teacher.school.strip().lower(),
        )
        return list(await session.scalars(query.order_by(User.class_name, User.name)))


async def students_for_class(class_name: str, school: str | None = None) -> list[User]:
    await ensure_database()
    if not school:
        return []
    async with async_session() as session:
        query = select(User).where(
            User.role == "student",
            func.lower(User.class_name) == class_name.strip().lower(),
        )
        query = query.where(func.lower(User.school) == school.strip().lower())
        return list(await session.scalars(query))


async def parent_children(parent: User) -> list[User]:
    await ensure_database()
    async with async_session() as session:
        links = await session.scalars(
            select(ParentStudentLink)
            .options(selectinload(ParentStudentLink.student))
            .where(
                ParentStudentLink.parent_id == parent.id,
                ParentStudentLink.status == LinkStatus.APPROVED,
            )
            .order_by(ParentStudentLink.created_at)
        )
        return [link.student for link in links]


async def parent_summary(
    parent: User, student_id: int | None = None
) -> tuple[User | None, int, int, list[Deadline]]:
    await ensure_database()
    async with async_session() as session:
        child_query = (
            select(User)
            .join(ParentStudentLink, ParentStudentLink.student_id == User.id)
            .where(
                ParentStudentLink.parent_id == parent.id,
                ParentStudentLink.status == LinkStatus.APPROVED,
            )
            .order_by(User.name)
        )
        if student_id is not None:
            child_query = child_query.where(User.id == student_id)
        child = await session.scalar(child_query.limit(1))
        if child is None:
            return None, 0, 0, []
        week_ago = datetime.now() - timedelta(days=7)
        completed = await session.scalar(
            select(func.count(Homework.id))
            .outerjoin(
                HomeworkCompletion,
                and_(
                    HomeworkCompletion.homework_id == Homework.id,
                    HomeworkCompletion.student_id == child.id,
                ),
            )
            .where(
                or_(
                    and_(
                        Homework.user_id == child.id,
                        Homework.status == "done",
                        Homework.completed_at >= week_ago,
                    ),
                    and_(
                        Homework.class_name == child.class_name,
                        Homework.created_by.in_(
                            select(User.id).where(
                                User.role == "teacher", User.school == child.school
                            )
                        ),
                        HomeworkCompletion.id.is_not(None),
                        HomeworkCompletion.completed_at >= week_ago,
                    ),
                ),
            )
        )
        overdue = await session.scalar(
            select(func.count(Homework.id)).where(
                Homework.deadline < datetime.now(),
                Homework.status == "active",
                or_(
                    and_(
                        Homework.user_id == child.id,
                        ~select(HomeworkCompletion.id)
                        .where(
                            HomeworkCompletion.homework_id == Homework.id,
                            HomeworkCompletion.student_id == child.id,
                        )
                        .exists(),
                    ),
                    and_(
                        Homework.class_name == child.class_name,
                        Homework.created_by.in_(
                            select(User.id).where(
                                User.role == "teacher", User.school == child.school
                            )
                        ),
                        ~select(HomeworkCompletion.id)
                        .where(
                            HomeworkCompletion.homework_id == Homework.id,
                            HomeworkCompletion.student_id == child.id,
                        )
                        .exists(),
                    ),
                ),
            )
        )
        deadlines = list(
            await session.scalars(
                select(Deadline)
                .where(
                    Deadline.date >= datetime.now(),
                    or_(
                        Deadline.user_id == child.id,
                        and_(
                            Deadline.class_name == child.class_name,
                            Deadline.created_by.in_(
                                select(User.id).where(
                                    User.role == "teacher",
                                    User.school == child.school,
                                )
                            ),
                        ),
                    ),
                )
                .order_by(Deadline.date)
                .limit(5)
            )
        )
        return child, completed or 0, overdue or 0, deadlines


async def unlock_reward(user: User, item_key: str, cost: int) -> bool:
    await ensure_database()
    async with async_session() as session:
        existing = await session.scalar(
            select(Reward.id).where(
                Reward.user_id == user.id, Reward.item_key == item_key
            )
        )
        user_row = await session.get(User, user.id)
        if existing is not None or user_row is None or user_row.points < cost:
            return False
        user_row.points -= cost
        session.add(Reward(user_id=user.id, item_key=item_key))
        await session.commit()
        return True


async def create_user_schedule_for_class(
    teacher: User, class_name: str, day: int, lesson_time: time, subject: str, room: str | None
) -> ScheduleItem:
    return await create_schedule_item(
        owner_id=teacher.id,
        creator_id=teacher.id,
        day_of_week=day,
        lesson_time=lesson_time,
        subject=subject,
        room=room,
        class_name=class_name,
    )


async def today_counts_for_teacher(teacher: User) -> tuple[int, int]:
    await ensure_database()
    async with async_session() as session:
        classes = [value.strip() for value in (teacher.classes or "").split(",") if value.strip()]
        if not classes:
            return 0, 0
        active_ids = select(Homework.id).where(
            Homework.created_by == teacher.id,
            Homework.class_name.in_(classes),
            Homework.status == "active",
        )
        assigned = await session.scalar(
            select(func.count(Homework.id)).where(Homework.id.in_(active_ids))
        )
        completed = await session.scalar(
            select(func.count(HomeworkCompletion.id)).where(
                HomeworkCompletion.homework_id.in_(active_ids)
            )
        )
        return assigned or 0, completed or 0


async def teacher_completion_report(teacher: User) -> list[tuple[str, int, int]]:
    await ensure_database()
    students = await class_students(teacher)
    classes = [value.strip() for value in (teacher.classes or "").split(",") if value.strip()]
    if not students or not classes:
        return []
    async with async_session() as session:
        assignments = list(
            await session.scalars(
                select(Homework).where(
                    Homework.created_by == teacher.id,
                    Homework.class_name.in_(classes),
                    Homework.status == "active",
                )
            )
        )
        result = []
        for student in students:
            eligible = [
                item for item in assignments if item.class_name == student.class_name
            ]
            if not eligible:
                continue
            completed = await session.scalar(
                select(func.count(HomeworkCompletion.id)).where(
                    HomeworkCompletion.student_id == student.id,
                    HomeworkCompletion.homework_id.in_(
                        [item.id for item in eligible]
                    ),
                )
            )
            result.append(
                (
                    student.name or f"Ученик #{student.id}",
                    completed or 0,
                    len(eligible),
                )
            )
        return result


async def reminder_already_delivered(user_id: int, delivery_key: str) -> bool:
    await ensure_database()
    async with async_session() as session:
        return (
            await session.scalar(
                select(ReminderDelivery.id).where(
                    ReminderDelivery.user_id == user_id,
                    ReminderDelivery.delivery_key == delivery_key,
                )
            )
        ) is not None


async def record_reminder_delivery(user_id: int, delivery_key: str) -> None:
    await ensure_database()
    async with async_session() as session:
        session.add(
            ReminderDelivery(user_id=user_id, delivery_key=delivery_key)
        )
        await session.commit()


async def reminder_users() -> list[User]:
    await ensure_database()
    async with async_session() as session:
        return list(
            await session.scalars(
                select(User).where(
                    User.reminders_enabled.is_(True),
                    User.role.in_(("student", "parent")),
                )
            )
        )


async def mood_was_logged_today(user_id: int) -> bool:
    await ensure_database()
    async with async_session() as session:
        return (
            await session.scalar(
                select(MoodEntry.id).where(
                    MoodEntry.user_id == user_id,
                    MoodEntry.date == date.today(),
                )
            )
        ) is not None


async def deadlines_for_parent(parent: User) -> list[Deadline]:
    await ensure_database()
    async with async_session() as session:
        children_query = (
            select(User)
            .join(ParentStudentLink, ParentStudentLink.student_id == User.id)
            .where(
                ParentStudentLink.parent_id == parent.id,
                ParentStudentLink.status == LinkStatus.APPROVED,
            )
        )
        children = list(await session.scalars(children_query))
        if not children:
            return []
        conditions = []
        for child in children:
            conditions.append(Deadline.user_id == child.id)
            if child.class_name and child.school:
                conditions.append(
                    and_(
                        Deadline.class_name == child.class_name,
                        Deadline.created_by.in_(
                            select(User.id).where(
                                User.role == "teacher", User.school == child.school
                            )
                        ),
                    )
                )
        return list(
            await session.scalars(
                select(Deadline)
                .where(Deadline.date >= datetime.now(), or_(*conditions))
                .order_by(Deadline.date)
            )
        )


async def psychologist_signals(psychologist_id: int) -> list[StressSignal]:
    await ensure_database()
    async with async_session() as session:
        return list(
            await session.scalars(
                select(StressSignal)
                .options(selectinload(StressSignal.student))
                .where(StressSignal.psychologist_id == psychologist_id)
                .order_by(StressSignal.created_at.desc())
            )
        )


async def psychologist_signal(
    psychologist_id: int, signal_id: int
) -> StressSignal | None:
    await ensure_database()
    async with async_session() as session:
        return await session.scalar(
            select(StressSignal)
            .options(selectinload(StressSignal.student))
            .where(
                StressSignal.id == signal_id,
                StressSignal.psychologist_id == psychologist_id,
            )
        )


async def psychologist_signals_summary(psychologist_id: int) -> dict[str, int]:
    await ensure_database()
    async with async_session() as session:
        total = await session.scalar(
            select(func.count(StressSignal.id)).where(
                StressSignal.psychologist_id == psychologist_id
            )
        )
        pending = await session.scalar(
            select(func.count(StressSignal.id)).where(
                StressSignal.psychologist_id == psychologist_id,
                StressSignal.status == "pending",
            )
        )
        in_progress = await session.scalar(
            select(func.count(StressSignal.id)).where(
                StressSignal.psychologist_id == psychologist_id,
                StressSignal.status == "in_progress",
            )
        )
        closed = await session.scalar(
            select(func.count(StressSignal.id)).where(
                StressSignal.psychologist_id == psychologist_id,
                StressSignal.status == "closed",
            )
        )
        return {
            "total": total or 0,
            "pending": pending or 0,
            "in_progress": in_progress or 0,
            "closed": closed or 0,
        }


async def update_signal_status(signal_id: int, status: str) -> bool:
    await ensure_database()
    async with async_session() as session:
        signal = await session.get(StressSignal, signal_id)
        if signal is None:
            return False
        signal.status = status
        await session.commit()
        return True


async def psychologist_for_school(school_name: str) -> User | None:
    """Психолог школы ученика: сравнение названий без учёта регистра и «№».

    Названия школ пользователи вводят вручную, поэтому точное сравнение строк
    регулярно не срабатывало («Школа №1» ≠ «школа N 1»), и ученик не мог
    связаться с психологом. Сравниваем нормализованные названия, а если
    точного совпадения нет — ищем частичное («МБОУ Школа №1» ⊃ «школа 1»).
    """
    await ensure_database()
    target = normalize_school(school_name)
    if not target:
        return None
    async with async_session() as session:
        rows = list(
            await session.scalars(
                select(User)
                .where(
                    User.role == "psychologist",
                    User.status == "approved",
                    User.school_name.isnot(None),
                )
                .order_by(User.id)
            )
        )
    exact = [row for row in rows if normalize_school(row.school_name) == target]
    if exact:
        return exact[0]
    partial = [
        row
        for row in rows
        if target in normalize_school(row.school_name)
        or normalize_school(row.school_name) in target
    ]
    return partial[0] if partial else None


async def psychologist_schools() -> list[str]:
    """Школы, в которых зарегистрирован одобренный психолог.

    Список нужен, чтобы ученик выбирал школу кнопками, а не вводил её вручную:
    так сигнал гарантированно доходит до психолога.
    """
    await ensure_database()
    async with async_session() as session:
        rows = list(
            await session.scalars(
                select(User.school_name)
                .where(
                    User.role == "psychologist",
                    User.status == "approved",
                    User.school_name.isnot(None),
                )
                .order_by(User.school_name)
            )
        )
    seen: dict[str, str] = {}
    for name in rows:
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        seen.setdefault(normalize_school(cleaned), cleaned)
    return sorted(seen.values(), key=lambda value: normalize_school(value))


async def any_psychologist() -> User | None:
    """Любой одобренный психолог — запасной вариант, если в школе ученика его нет.

    Используется только с явного согласия ученика (кнопка «Передать дежурному
    психологу»), чтобы обращение не осталось без ответа.
    """
    await ensure_database()
    async with async_session() as session:
        return await session.scalar(
            select(User)
            .where(User.role == "psychologist", User.status == "approved")
            .order_by(User.id)
        )


async def award_points(user_id: int, amount: int) -> int:
    """Начислить (или списать при отрицательном значении) очки. Возвращает баланс."""
    await ensure_database()
    async with async_session() as session:
        row = await session.get(User, user_id)
        if row is None:
            return 0
        row.points = max(0, int(row.points) + int(amount))
        await session.commit()
        return row.points


async def daily_spin(user: User) -> int | None:
    """Один спин колеса удачи в сутки. None — сегодня уже использован.

    Доступен владельцам награды «🎰 Колесо удачи». Факт спина запоминается в
    ReminderDelivery (ключ ``spin:<дата>``), поэтому повторно очки не выдаются.
    """
    await ensure_database()
    keys = await user_reward_keys(user.id)
    if "daily_spin" not in keys:
        return None
    delivery_key = f"spin:{date.today():%Y-%m-%d}"
    if await reminder_already_delivered(user.id, delivery_key):
        return None
    prize = random.choice(SPIN_PRIZES)
    await record_reminder_delivery(user.id, delivery_key)
    if prize:
        await award_points(user.id, prize)
    return prize


async def create_stress_signal(
    student_id: int,
    psychologist_id: int,
    risk_level: int,
    themes: list[str],
    preferred_format: str,
    consent: bool = True,
) -> StressSignal:
    """Создать сигнал психологу. ``consent`` — реальное согласие ученика.

    Согласие передаётся вызывающей стороной (кнопка «Передать психологу»),
    а не подставляется молча: поле ``StressSignal.consent`` должно отражать
    осознанное действие ученика, иначе оно вводит психолога в заблуждение.
    """
    await ensure_database()
    async with async_session() as session:
        signal = StressSignal(
            student_id=student_id,
            psychologist_id=psychologist_id,
            risk_level=risk_level,
            themes=", ".join(themes),
            status="pending",
            consent=consent,
            preferred_format=preferred_format,
        )
        session.add(signal)
        await session.commit()
        await session.refresh(signal)
        return signal


async def has_open_signal(student_id: int, psychologist_id: int) -> bool:
    """Есть ли уже незакрытый сигнал этому психологу от этого ученика.

    Без этой проверки ученик мог нажать «Передать сигнал» несколько раз и
    засыпать психолога одинаковыми заявками, а очередь «📥 Сигналы» теряла
    смысл.
    """
    await ensure_database()
    async with async_session() as session:
        existing = await session.scalar(
            select(StressSignal.id).where(
                StressSignal.student_id == student_id,
                StressSignal.psychologist_id == psychologist_id,
                StressSignal.status.in_(("pending", "in_progress")),
            )
        )
        return existing is not None


async def _homework_gain(session, student_id: int, item: Homework) -> int:
    """Баллы за сдачу задания: стоимость домашки × буст наград.

    Асинхронный помощник: вызывается внутри открытой сессии mvp-функций.
    Награды читаются одним запросом; «🚀 Мегабуст ×3» имеет приоритет над
    «⚡ Буст ×2».
    """
    gain = max(0, int(getattr(item, "points", 0) or 0))
    keys = set(
        await session.scalars(
            select(Reward.item_key).where(Reward.user_id == student_id)
        )
    )
    if "points_boost_3" in keys:
        return gain * 3
    if "points_boost" in keys:
        return gain * 2
    return gain


async def user_reward_keys(user_id: int) -> set[str]:
    """Ключи уже открытых пользователем наград."""
    await ensure_database()
    async with async_session() as session:
        rows = await session.scalars(
            select(Reward.item_key).where(Reward.user_id == user_id)
        )
        return set(rows)


async def parents_of_class_students(
    class_name: str | None, school: str | None = None
) -> list[User]:
    """Родители учеников класса с подтверждённой связью и включёнными уведомлениями."""
    await ensure_database()
    if not class_name:
        return []
    async with async_session() as session:
        student_ids = select(User.id).where(
            User.role == "student",
            func.lower(User.class_name) == class_name.strip().lower(),
        )
        if school:
            student_ids = student_ids.where(
                func.lower(User.school) == school.strip().lower()
            )
        rows = await session.scalars(
            select(User)
            .join(ParentStudentLink, ParentStudentLink.parent_id == User.id)
            .where(
                ParentStudentLink.student_id.in_(student_ids),
                ParentStudentLink.status == LinkStatus.APPROVED,
                User.reminders_enabled.is_(True),
            )
            .distinct()
        )
        return list(rows)
