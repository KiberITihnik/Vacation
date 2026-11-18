"""Работа с БД: SQLite по умолчанию, через SQLAlchemy 2."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    DeliveryRow,
    FeedbackRow,
    ProfileStateRow,
    RunRow,
    Vacancy,
    VacancyRow,
    utcnow,
)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class Database:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions() as s, s.begin():
            yield s

    # ------------------------------------------------------- vacancies
    def upsert_vacancy(self, v: Vacancy, fingerprint: str) -> int:
        data = v.model_dump(mode="json", exclude={"raw", "db_id", "warnings", "score", "links"})
        with self.session() as s:
            row = s.scalar(
                select(VacancyRow).where(
                    VacancyRow.source == v.source, VacancyRow.external_id == v.external_id
                )
            )
            if row is None:
                row = VacancyRow(source=v.source, external_id=v.external_id)
                s.add(row)
            row.fingerprint = fingerprint
            row.title = v.title
            row.company = v.company
            row.city = v.city
            row.url = v.url
            row.salary_from = v.salary_from
            row.salary_to = v.salary_to
            row.work_format = v.work_format
            row.experience = v.experience
            row.published_at = v.published_at
            row.data_json = data
            row.raw_json = v.raw
            s.flush()
            return row.id

    def get_vacancy(self, vacancy_id: int) -> Vacancy | None:
        with self.session() as s:
            row = s.get(VacancyRow, vacancy_id)
            if row is None:
                return None
            v = Vacancy.model_validate(row.data_json)
            v.db_id = row.id
            return v

    # ------------------------------------------------------- deliveries
    def delivered(self, profile_id: str) -> tuple[set[int], set[str]]:
        with self.session() as s:
            rows = s.execute(
                select(DeliveryRow.vacancy_id, DeliveryRow.fingerprint).where(
                    DeliveryRow.profile_id == profile_id
                )
            ).all()
        return {r[0] for r in rows}, {r[1] for r in rows}

    def record_delivery(
        self,
        profile_id: str,
        vacancy_id: int,
        fingerprint: str,
        message_id: int | None,
        score: float,
    ) -> None:
        with self.session() as s:
            exists = s.scalar(
                select(DeliveryRow.id).where(
                    DeliveryRow.profile_id == profile_id, DeliveryRow.vacancy_id == vacancy_id
                )
            )
            if exists is None:
                s.add(
                    DeliveryRow(
                        profile_id=profile_id,
                        vacancy_id=vacancy_id,
                        fingerprint=fingerprint,
                        message_id=message_id,
                        score=score,
                    )
                )

    def top_delivered(self, profile_id: str, days: int = 7, limit: int = 10) -> list[Vacancy]:
        since = utcnow() - timedelta(days=days)
        with self.session() as s:
            rows = s.execute(
                select(VacancyRow, DeliveryRow.score)
                .join(DeliveryRow, DeliveryRow.vacancy_id == VacancyRow.id)
                .where(DeliveryRow.profile_id == profile_id, DeliveryRow.sent_at >= since)
                .order_by(DeliveryRow.score.desc())
                .limit(limit)
            ).all()
        result = []
        for row, score in rows:
            v = Vacancy.model_validate(row.data_json)
            v.db_id, v.score = row.id, score
            result.append(v)
        return result

    # ------------------------------------------------------- feedback
    def add_feedback(self, profile_id: str, vacancy_id: int, action: str) -> None:
        with self.session() as s:
            s.add(FeedbackRow(profile_id=profile_id, vacancy_id=vacancy_id, action=action))

    def feedback_companies(self, profile_id: str, action: str = "hide_company") -> set[str]:
        with self.session() as s:
            rows = s.scalars(
                select(VacancyRow.company)
                .join(FeedbackRow, FeedbackRow.vacancy_id == VacancyRow.id)
                .where(FeedbackRow.profile_id == profile_id, FeedbackRow.action == action)
            ).all()
        return set(rows)

    def feedback_titles(self, profile_id: str, action: str = "dislike") -> list[str]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(VacancyRow.title)
                    .join(FeedbackRow, FeedbackRow.vacancy_id == VacancyRow.id)
                    .where(FeedbackRow.profile_id == profile_id, FeedbackRow.action == action)
                ).all()
            )

    # ------------------------------------------------------- profile state
    def get_state(self, profile_id: str) -> ProfileStateRow:
        with self.session() as s:
            row = s.get(ProfileStateRow, profile_id)
            if row is None:
                row = ProfileStateRow(profile_id=profile_id, paused=False)
                s.add(row)
                s.flush()
            row.last_manual_run_at = _aware(row.last_manual_run_at)
            return row

    def update_state(self, profile_id: str, **fields: object) -> None:
        with self.session() as s:
            row = s.get(ProfileStateRow, profile_id)
            if row is None:
                row = ProfileStateRow(profile_id=profile_id, paused=False)
                s.add(row)
            for k, val in fields.items():
                setattr(row, k, val)

    # ------------------------------------------------------- runs
    def add_run(self, row: RunRow) -> None:
        with self.session() as s:
            s.add(row)

    def last_runs(self, profile_id: str, source: str, n: int = 3) -> list[RunRow]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(RunRow)
                    .where(
                        RunRow.profile_id == profile_id,
                        RunRow.source == source,
                        RunRow.dry_run.is_(False),
                    )
                    .order_by(RunRow.id.desc())
                    .limit(n)
                ).all()
            )

    def stats(self, days: int = 7) -> list[tuple[str, str, int, int, int, int]]:
        """(profile, source, runs, found, sent, errors) за последние дни."""
        since = utcnow() - timedelta(days=days)
        with self.session() as s:
            rows = s.execute(
                select(
                    RunRow.profile_id,
                    RunRow.source,
                    func.count(),
                    func.sum(RunRow.found),
                    func.sum(RunRow.sent),
                    func.count(RunRow.error),
                )
                .where(RunRow.started_at >= since, RunRow.dry_run.is_(False))
                .group_by(RunRow.profile_id, RunRow.source)
            ).all()
        return [(r[0], r[1], r[2], r[3] or 0, r[4] or 0, r[5]) for r in rows]
