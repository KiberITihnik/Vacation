"""Дедупликация: точная по (source, external_id) и межисточниковая по fingerprint + rapidfuzz."""

from __future__ import annotations

from rapidfuzz import fuzz

from app.models import Link, Vacancy
from app.processing.normalize import norm_city, norm_company, norm_text

TITLE_SIMILARITY = 90


def fingerprint(v: Vacancy) -> str:
    return "|".join((norm_company(v.company), norm_text(v.title), norm_city(v.city)))


def _richness(v: Vacancy) -> tuple[int, int, int]:
    """Какую карточку оставить при слиянии: с зарплатой, с описанием, свежее."""
    has_salary = int(bool(v.salary_from or v.salary_to))
    ts = int(v.published_at.timestamp()) if v.published_at else 0
    return has_salary, len(v.description), ts


def _same(a: Vacancy, b: Vacancy) -> bool:
    if a.source == b.source and a.external_id == b.external_id:
        return True
    ca, cb = norm_company(a.company), norm_company(b.company)
    if not ca or ca != cb:
        return False
    if norm_city(a.city) != norm_city(b.city):
        return False
    return fuzz.token_sort_ratio(norm_text(a.title), norm_text(b.title)) >= TITLE_SIMILARITY


def dedupe(vacancies: list[Vacancy]) -> list[Vacancy]:
    """Склеивает дубликаты в одну карточку, собирая ссылки всех источников."""
    groups: list[list[Vacancy]] = []
    index: dict[tuple[str, str], int] = {}
    for v in vacancies:
        key = (norm_company(v.company), norm_city(v.city))
        exact = (v.source, v.external_id)
        if exact in index:
            continue
        placed = False
        for gi, group in enumerate(groups):
            head = group[0]
            if (norm_company(head.company), norm_city(head.city)) == key and _same(head, v):
                group.append(v)
                index[exact] = gi
                placed = True
                break
        if not placed:
            groups.append([v])
            index[exact] = len(groups) - 1

    result: list[Vacancy] = []
    for group in groups:
        best = max(group, key=_richness).model_copy(deep=True)
        links: list[Link] = []
        seen: set[str] = set()
        for v in [best, *group]:
            for link in v.all_links():
                if link.url not in seen:
                    seen.add(link.url)
                    links.append(link)
        best.links = links
        for v in group:
            for w in v.warnings:
                if w not in best.warnings:
                    best.warnings.append(w)
        result.append(best)
    return result
