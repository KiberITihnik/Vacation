"""Антимошеннический фильтр. Правила — в config/profiles.yaml → antiscam."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.config import AntiscamConfig
from app.models import Vacancy
from app.processing.normalize import norm_text

Verdict = Literal["ok", "warn", "hide"]


@dataclass(frozen=True)
class Rule:
    source: str
    parts: tuple[str, ...] = ()
    regex: re.Pattern[str] | None = None

    @classmethod
    def parse(cls, raw: str) -> Rule:
        if raw.startswith("re:"):
            return cls(raw, regex=re.compile(raw[3:], re.IGNORECASE))
        return cls(raw, parts=tuple(norm_text(p) for p in raw.split("+") if p.strip()))

    def matches(self, text_raw: str, text_norm: str) -> bool:
        if self.regex is not None:
            return bool(self.regex.search(text_raw))
        return all(p in text_norm for p in self.parts)


class AntiScam:
    def __init__(self, cfg: AntiscamConfig) -> None:
        self.hide = [Rule.parse(r) for r in cfg.hide]
        self.warn = [Rule.parse(r) for r in cfg.warn]

    def check(self, v: Vacancy) -> tuple[Verdict, list[str]]:
        raw = f"{v.title}\n{v.company}\n{v.description}\n{v.salary_text}".lower()
        raw = raw.replace("ё", "е")
        norm = norm_text(raw)
        hits = [r.source for r in self.hide if r.matches(raw, norm)]
        if hits:
            return "hide", hits
        warns = [r.source for r in self.warn if r.matches(raw, norm)]
        if not v.company.strip():
            warns.append("не указана компания")
        return ("warn", warns) if warns else ("ok", [])

    def apply(self, vacancies: list[Vacancy]) -> tuple[list[Vacancy], list[Vacancy]]:
        """Возвращает (оставленные, скрытые). У сомнительных заполняется warnings."""
        kept, hidden = [], []
        for v in vacancies:
            verdict, reasons = self.check(v)
            if verdict == "hide":
                v.warnings = reasons
                hidden.append(v)
                continue
            if verdict == "warn":
                v.warnings = [*v.warnings, *(r for r in reasons if r not in v.warnings)]
            kept.append(v)
        return kept, hidden
