"""Фильтр вакансий под профиль: ключевые слова, стоп-слова, формат, город, опыт, зарплата."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.config import ProfileConfig
from app.models import Vacancy
from app.processing.normalize import norm_city, norm_company, norm_text

# В источниках со свободным текстом (Telegram) ключевые слова ищем и в начале поста.
TEXT_MATCH_CHARS = 300


@dataclass
class FilterResult:
    kept: list[Vacancy] = field(default_factory=list)
    freelance: list[Vacancy] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)


def _stem(word: str) -> str:
    """Грубая основа слова: «менеджера» и «менеджер» совпадут, «smm» — целиком."""
    return word if len(word) <= 4 else word[: max(4, len(word) - 2)]


def phrase_in(phrase: str, text: str) -> bool:
    """Все слова фразы (с учётом окончаний) встречаются в тексте подряд."""
    p_words = norm_text(phrase).split()
    t_words = norm_text(text).split()
    if not p_words or not t_words:
        return False
    stems = [_stem(w) for w in p_words]
    n = len(stems)
    for i in range(len(t_words) - n + 1):
        if all(t_words[i + j].startswith(stems[j]) for j in range(n)):
            return True
    return False


def keyword_hits(
    profile: ProfileConfig, v: Vacancy, *, text_sources: frozenset[str] = frozenset()
) -> tuple[int, int]:
    """(совпадения в названии, совпадения в описании)."""
    title = v.title
    if v.source in text_sources:
        title = f"{v.title} {v.description[:TEXT_MATCH_CHARS]}"
    kws = profile.include_keywords
    return (
        sum(1 for k in kws if phrase_in(k, title)),
        sum(1 for k in kws if phrase_in(k, v.description)),
    )


class ProfileFilter:
    def __init__(
        self,
        profile: ProfileConfig,
        *,
        min_salary: int | None = None,
        hidden_companies: set[str] | None = None,
        text_sources: frozenset[str] = frozenset(),
    ) -> None:
        self.p = profile
        self.min_salary = profile.min_salary if min_salary is None else min_salary
        self.hidden = {norm_company(c) for c in (hidden_companies or set()) if c}
        self.cities = {norm_city(c) for c in profile.locations.cities}
        self.excl = [k for k in profile.exclude_keywords if k.strip()]
        self.text_sources = text_sources

    def reason(self, v: Vacancy) -> str | None:
        """Причина отказа или None, если вакансия подходит."""
        if any(phrase_in(k, v.title) for k in self.excl):
            return "stop_word"
        if self.hidden and norm_company(v.company) in self.hidden:
            return "hidden_company"

        loc = self.p.locations
        if v.work_format == "remote":
            if not loc.allow_remote:
                return "format"
        else:
            # офис, гибрид и неизвестный формат — только в своих городах
            if not loc.allow_office:
                return "format"
            if self.cities and norm_city(v.city) not in self.cities:
                return "city"

        if v.experience not in self.p.experience:
            return "experience"

        salary_ok_for_any = (
            self.p.any_job_if_salary_from is not None
            and (v.salary_ref or 0) >= self.p.any_job_if_salary_from
            and v.salary_period != "piece"
        )
        title_hits, _ = keyword_hits(self.p, v, text_sources=self.text_sources)
        if self.p.include_keywords and not title_hits and not salary_ok_for_any:
            return "keywords"

        if (
            v.salary_period != "piece"
            and v.salary_max is not None
            and self.min_salary
            and v.salary_max < self.min_salary
        ):
            return "salary"
        return None

    def apply(self, vacancies: list[Vacancy]) -> FilterResult:
        res = FilterResult()
        for v in vacancies:
            r = self.reason(v)
            if r:
                res.rejected[r] += 1
                continue
            if v.experience in self.p.warn_experience:
                note = f"опыт {v.experience} лет"
                if note not in v.warnings:
                    v.warnings.append(note)
            if v.salary_period == "piece":
                v.freelance = True
                if self.p.show_freelance_block:
                    res.freelance.append(v)
                    continue
            res.kept.append(v)
        return res
