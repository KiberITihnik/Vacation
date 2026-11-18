"""Нормализация зарплаты, опыта, формата работы и текста."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.config import MoneyConfig
from app.models import Experience, SalaryPeriod, WorkFormat

SHIFTS_PER_MONTH = 15

_NUM_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[\s  ]\d{3})+|\d+(?:[.,]\d+)?)"
    r"\s*(?P<suffix>тыс\.?|т\.\s?р\.?|тр\b|к\b|k\b|млн)?",
    re.IGNORECASE,
)
_PIECE_RE = re.compile(
    r"за\s+(?:\d+\s*)?(?:стать|текст|символ|знак|зн\.|заказ|пост|слово|единиц|штук|шт\b|задани)"
    r"|сдельн|/\s*(?:знак|1000|стать|текст)",
    re.IGNORECASE,
)
_HOUR_RE = re.compile(r"(?:/|в|за)\s*(?:час|ч\b)|почасов", re.IGNORECASE)
_SHIFT_RE = re.compile(r"(?:/|в|за)\s*смен", re.IGNORECASE)
_USD_RE = re.compile(r"\$|usd|долл", re.IGNORECASE)
_EUR_RE = re.compile(r"€|eur|евро", re.IGNORECASE)


@dataclass(frozen=True)
class SalaryInfo:
    salary_from: int | None = None
    salary_to: int | None = None
    net: bool | None = None
    period: SalaryPeriod = "unknown"


def _to_number(raw: str, suffix: str | None) -> float:
    value = float(re.sub(r"[\s  ]", "", raw).replace(",", "."))
    if suffix:
        s = suffix.lower()
        if s.startswith("млн"):
            value *= 1_000_000
        else:
            value *= 1000
    return value


def detect_net(text: str) -> bool | None:
    t = text.lower()
    if "на руки" in t or "после вычета" in t or re.search(r"\bnet\b", t):
        return True
    if "до вычета" in t or re.search(r"\bgross\b", t) or "до налог" in t:
        return False
    return None


def parse_salary(text: str | None, money: MoneyConfig | None = None) -> SalaryInfo:
    """Разбор строки вида «от 50 000», «50–70 тыс.», «$1000», «до 120к», «200 ₽/час»."""
    if not text or not text.strip():
        return SalaryInfo()
    money = money or MoneyConfig()
    net = detect_net(text)
    if _PIECE_RE.search(text):
        return SalaryInfo(net=net, period="piece")

    matches = list(_NUM_RE.finditer(text))
    if not matches:
        return SalaryInfo(net=net)
    values = [_to_number(m["num"], m["suffix"]) for m in matches]
    # «50–70 тыс.»: суффикс тысяч у последнего числа распространяется на предыдущие
    if any(m["suffix"] for m in matches):
        values = [
            v * 1000 if v < 1000 and not m["suffix"] else v
            for v, m in zip(values, matches, strict=True)
        ]

    rate = 1.0
    if _USD_RE.search(text):
        rate = money.usd_rate
    elif _EUR_RE.search(text):
        rate = money.eur_rate
    values = [v * rate for v in values]

    period: SalaryPeriod = "month"
    if _HOUR_RE.search(text):
        period = "hour"
        values = [v * money.hours_per_month for v in values]
    elif _SHIFT_RE.search(text):
        period = "shift"
        values = [v * SHIFTS_PER_MONTH for v in values]

    ints = [int(round(v)) for v in values][:2]
    if len(ints) == 2:
        lo, hi = sorted(ints)
        return SalaryInfo(lo, hi, net, period)
    before = text[: matches[0].start()].lower()
    if re.search(r"\bдо\s*$", before):
        return SalaryInfo(None, ints[0], net, period)
    return SalaryInfo(ints[0], None, net, period)


def salary_from_numbers(
    lo: float | int | None,
    hi: float | int | None,
    currency: str | None = "rub",
    money: MoneyConfig | None = None,
) -> tuple[int | None, int | None]:
    """Для структурированных ответов API: 0 и None считаются «не указано»."""
    money = money or MoneyConfig()
    cur = (currency or "rub").lower()
    rate = money.usd_rate if cur in ("usd", "$") else money.eur_rate if cur in ("eur", "€") else 1
    a = int(lo * rate) if lo else None
    b = int(hi * rate) if hi else None
    if a and b and a > b:
        a, b = b, a
    return a, b


def parse_experience(value: str | int | None) -> Experience:
    if value is None:
        return "unknown"
    if isinstance(value, int):
        if value <= 0:
            return "none"
        if value < 3:
            return "1-3"
        if value < 6:
            return "3-6"
        return "6+"
    t = value.lower().replace("ё", "е")
    if not t.strip():
        return "unknown"
    if t.strip().isdigit():
        return parse_experience(int(t.strip()))
    if any(w in t for w in ("без опыта", "не требуется", "нет опыта", "noexperience", "не важен")):
        return "none"
    if "between1and3" in t or re.search(r"\b1\s*[-–—]\s*3\b|от 1 год|от года|1 год", t):
        return "1-3"
    if "between3and6" in t or re.search(r"\b3\s*[-–—]\s*6\b|от 3 лет|от 3-х лет", t):
        return "3-6"
    if "morethan6" in t or re.search(r"более 6|от 6 лет|6\+", t):
        return "6+"
    return "unknown"


_REMOTE_RE = re.compile(r"удал[её]н|дистанц|remote|из дома|на дому|work from home", re.I)
_HYBRID_RE = re.compile(r"гибрид|hybrid|частично удал", re.I)
_OFFICE_RE = re.compile(r"офис|полный (рабочий )?день|сменный график|на территории", re.I)


def parse_work_format(*texts: str | None, default: WorkFormat = "unknown") -> WorkFormat:
    joined = " ".join(t for t in texts if t)
    if not joined:
        return default
    if _HYBRID_RE.search(joined):
        return "hybrid"
    if _REMOTE_RE.search(joined):
        return "remote"
    if _OFFICE_RE.search(joined):
        return "office"
    return default


def detect_training(*texts: str | None) -> bool:
    joined = " ".join(t for t in texts if t).lower()
    return bool(re.search(r"обучени|обучаем|обучим|стажировк|наставни", joined))


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def norm_text(text: str | None) -> str:
    """Для сравнения ключевых слов: нижний регистр, ё→е, дефисы и пунктуация → пробел."""
    if not text:
        return ""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[\-–—_/\\.,:;!?()\"'«»]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_LEGAL_RE = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|нко|ано|гбу|мбу|фгуп|llc|ltd|inc)\b", re.I)


def norm_company(name: str | None) -> str:
    t = norm_text(name)
    t = _LEGAL_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


_CITY_RE = re.compile(
    r"(?:^|[\s,])г\.?\s*([А-ЯЁ][а-яё]+(?:-[а-яё]+-[А-ЯЁ][а-яё]+|[\s-][А-ЯЁ][а-яё]+)?)"
)


def city_from_address(address: str | None) -> str:
    if not address:
        return ""
    m = _CITY_RE.search(address)
    if m:
        return m.group(1).split(",")[0].strip()
    return ""


def norm_city(city: str | None) -> str:
    return norm_text(city).removeprefix("г ").strip()
