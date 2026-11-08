from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym


class Base(DeclarativeBase):
    pass


class UserRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    PARENT = "parent"
    PSYCHOLOGIST = "psychologist"
    UNSELECTED = "unselected"


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class LinkStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _enum_values(enum_type: type[Enum]) -> list[str]:
    return [item.value for item in enum_type]


class User(Base):
    __tablename__ = "users"

    def __init__(self, **kwargs: object) -> None:
        # ``name`` — синоним ``full_name``: значение по умолчанию ставим только
        # если имя не передано ни в одном из двух вариантов. Иначе явный
        # ``full_name=`` перезаписывался заглушкой «Пользователь».
        if "name" not in kwargs and "full_name" not in kwargs:
            kwargs["name"] = "Пользователь"
        kwargs.setdefault("role", UserRole.UNSELECTED)
        kwargs.setdefault("status", RequestStatus.APPROVED)
        kwargs.setdefault("points", 0)
        kwargs.setdefault("reminders_enabled", True)
        super().__init__(**kwargs)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), default="Пользователь")
    name = synonym("full_name")
    school_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    school = synonym("school_name")
    role: Mapped[str] = mapped_column(
        SqlEnum(
            UserRole,
            name="user_role",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        default=UserRole.UNSELECTED,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        SqlEnum(
            RequestStatus,
            name="request_status",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        default=RequestStatus.APPROVED,
        nullable=False,
    )
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classes: Mapped[str | None] = mapped_column(Text, nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_7d: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_3d: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_1d: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mood_survey_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Легаси-колонка (раньше — выбор основного интерфейса). Функция убрана:
    # мини-приложение открывается кнопкой-ссылкой прямо из меню бота.
    ui_mode: Mapped[str] = mapped_column(
        String(16), default="chat", server_default="chat", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    owned_classes: Mapped[list[SchoolClass]] = relationship(
        back_populates="teacher", cascade="all, delete-orphan"
    )
    class_memberships: Mapped[list[ClassMember]] = relationship(
        back_populates="student", cascade="all, delete-orphan", foreign_keys="ClassMember.student_id"
    )
    parent_links: Mapped[list[ParentStudentLink]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys="ParentStudentLink.parent_id",
    )
    student_links: Mapped[list[ParentStudentLink]] = relationship(
        back_populates="student",
        cascade="all, delete-orphan",
        foreign_keys="ParentStudentLink.student_id",
    )


class SchoolClass(Base):
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    teacher: Mapped[User] = relationship(back_populates="owned_classes")
    members: Mapped[list[ClassMember]] = relationship(
        back_populates="school_class", cascade="all, delete-orphan"
    )


class ClassMember(Base):
    __tablename__ = "class_members"
    __table_args__ = (
        UniqueConstraint("class_id", "student_id", name="uq_class_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    school_class: Mapped[SchoolClass] = relationship(back_populates="members")
    student: Mapped[User] = relationship(
        back_populates="class_memberships", foreign_keys=[student_id]
    )


class ParentStudentLink(Base):
    __tablename__ = "parent_student_links"
    __table_args__ = (
        UniqueConstraint("parent_id", "student_id", name="uq_parent_student_link"),
        Index("ix_parent_student_status", "parent_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        SqlEnum(
            LinkStatus,
            name="link_status",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        default=LinkStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    parent: Mapped[User] = relationship(
        back_populates="parent_links", foreign_keys=[parent_id]
    )
    student: Mapped[User] = relationship(
        back_populates="student_links", foreign_keys=[student_id]
    )


class ScheduleItem(Base):
    __tablename__ = "schedule_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    time: Mapped[time] = mapped_column(Time, nullable=False)
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    room: Mapped[str | None] = mapped_column(String(64))
    class_name: Mapped[str | None] = mapped_column(String(64))


class Homework(Base):
    __tablename__ = "homework"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    class_name: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Баллы, которые ученик получает за сдачу этого задания (задаёт учитель).
    points: Mapped[int] = mapped_column(Integer, default=10, nullable=False)


class HomeworkCompletion(Base):
    __tablename__ = "homework_completions"
    __table_args__ = (
        UniqueConstraint("homework_id", "student_id", name="uq_homework_completion"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    homework_id: Mapped[int] = mapped_column(ForeignKey("homework.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    completed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Deadline(Base):
    __tablename__ = "deadlines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    class_name: Mapped[str | None] = mapped_column(String(64))


class MoodEntry(Base):
    __tablename__ = "mood_entries"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_mood_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    risk_score: Mapped[int | None] = mapped_column(Integer)
    themes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StressSignal(Base):
    __tablename__ = "stress_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    psychologist_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    themes: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    preferred_format: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    student: Mapped[User] = relationship(foreign_keys=[student_id])
    psychologist: Mapped[User | None] = relationship(foreign_keys=[psychologist_id])


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    class_name: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Reward(Base):
    __tablename__ = "rewards"
    __table_args__ = (UniqueConstraint("user_id", "item_key", name="uq_reward_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)


class ReminderDelivery(Base):
    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint("user_id", "delivery_key", name="uq_reminder_delivery"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    delivery_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
