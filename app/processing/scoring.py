"""Скоринг и сортировка. Веса — в config/profiles.yaml → scoring."""

from __future__ import annotations

from datetime import UTC, datetime

from rapidfuzz import fuzz

from app.config import ProfileConfig, ScoringConfig
from app.models import Vacancy
from app.processing.filters import keyword_hits
from app.processing.normalize import norm_text


def freshness(v: Vacancy, days: int, now: datetime | None = None) -> float:
    if v.published_at is None:
        return 0.3
    now = now or datetime.now(UTC)
    pub = v.published_at if v.published_at.tzinfo else v.published_at.replace(tzinfo=UTC)
    age = max(0.0, (now - pub).total_seconds() / 86400)
    return max(0.0, 1 - age / days) if days > 0 else 0.0


def score(
    v: Vacancy,
    profile: ProfileConfig,
    cfg: ScoringConfig,
    *,
    disliked_titles: list[str] | None = None,
    now: datetime | None = None,
) -> float:
    w = cfg.for_profile(profile.id)
    salary = min(1.0, (v.salary_ref or 0) / cfg.salary_cap) if cfg.salary_cap else 0.0
    t_hits, d_hits = keyword_hits(profile, v)
    kw = min(1.0, 0.7 * min(t_hits, 1) + 0.15 * min(t_hits - 1, 1) + 0.15 * min(d_hits, 1))
    remote = 1.0 if v.work_format == "remote" else 0.0
    s = (
        w.salary * salary
        + w.freshness * freshness(v, cfg.freshness_days, now)
        + w.keywords * kw
        + w.remote * remote
    )
    if v.warnings:
        s -= cfg.warn_penalty
    if disliked_titles:
        title = norm_text(v.title)
        best = max((fuzz.token_sort_ratio(title, norm_text(t)) for t in disliked_titles), default=0)
        if best >= 80:
            s -= cfg.dislike_penalty * best / 100
    return round(s, 4)


def rank(
    vacancies: list[Vacancy],
    profile: ProfileConfig,
    cfg: ScoringConfig,
    *,
    disliked_titles: list[str] | None = None,
    now: datetime | None = None,
) -> list[Vacancy]:
    for v in vacancies:
        v.score = score(v, profile, cfg, disliked_titles=disliked_titles, now=now)
    if profile.sort == "salary_desc":
        # с зарплатой — первыми, по убыванию; без зарплаты — после, по скорингу
        return sorted(
            vacancies,
            key=lambda v: (
                v.salary_ref is not None and not v.warnings,
                v.salary_ref or 0,
                v.score,
            ),
            reverse=True,
        )
    return sorted(vacancies, key=lambda v: v.score, reverse=True)
