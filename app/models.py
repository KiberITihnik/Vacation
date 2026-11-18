"""Доменная модель вакансии (pydantic) и таблицы БД (SQLAlchemy 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

WorkFormat = Literal["office", "hybrid", "remote", "unknown"]
Experience = Literal["none", "1-3", "3-6", "6+", "unknown"]
SalaryPeriod = Literal["month", "hour", "shift", "piece", "unknown"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Link(BaseModel):
    source: str
    url: str
    label: str = ""  # например, @канал для Telegram


class Vacancy(BaseModel):
    """Нормализованная вакансия, которую отдаёт любой адаптер источника."""

    source: str
    external_id: str
    title: str
    company: str = ""
    city: str = ""
    url: str
    published_at: datetime | None = None
    description: str = ""

    salary_from: int | None = None  # ₽ в месяц
    salary_to: int | None = None
    salary_net: bool | None = None  # True — «на руки», False — «до вычета», None — неизвестно
    salary_period: SalaryPeriod = "unknown"
    salary_text: str = ""  # исходная строка, нужна для сдельной оплаты

    work_format: WorkFormat = "unknown"
    experience: Experience = "unknown"
    training: bool = False

    raw: dict[str, Any] = Field(default_factory=dict)

    # заполняются конвейером
    db_id: int | None = None
    links: list[Link] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    score: float = 0.0
    freelance: bool = False

    def all_links(self) -> list[Link]:
        return self.links or [Link(source=self.source, url=self.url)]

    @property
    def salary_ref(self) -> int | None:
        """Ориентир для сортировки: середина вилки или единственная граница."""
        if self.salary_from and self.salary_to:
            return (self.salary_from + self.salary_to) // 2
        return self.salary_from or self.salary_to

    @property
    def salary_max(self) -> int | None:
        return self.salary_to or self.salary_from


# ---------------------------------------------------------------- ORM


class Base(DeclarativeBase):
    pass


class VacancyRow(Base):
    __tablename__ = "vacancies"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(String(512))
    company: Mapped[str] = mapped_column(String(512), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(String(1024))
    salary_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    work_format: Mapped[str] = mapped_column(String(16), default="unknown")
    experience: Mapped[str] = mapped_column(String(16), default="unknown")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON)  # нормализованная Vacancy
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("profile_id", "vacancy_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64), index=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    fingerprint: Mapped[str] = mapped_column(String(512), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    score: Mapped[float] = mapped_column(default=0.0)


class FeedbackRow(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64), index=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    action: Mapped[str] = mapped_column(String(16))  # like | dislike | applied | hide_company
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    profile_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))  # "*" — итог по профилю
    found: Mapped[int] = mapped_column(Integer, default=0)
    filtered: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)


class ProfileStateRow(Base):
    """Изменяемые из бота настройки профиля: пауза, своя минимальная зарплата."""

    __tablename__ = "profile_state"

    profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    min_salary: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_manual_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
