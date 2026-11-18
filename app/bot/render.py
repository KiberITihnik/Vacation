"""Формат карточки вакансии для Telegram (HTML parse mode)."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from app.models import Vacancy

SOURCE_TITLES = {
    "hh_email": "hh.ru",
    "trudvsem": "Работа России",
    "superjob": "SuperJob",
    "telegram_channels": "Telegram",
}
FORMAT_TITLES = {"office": "офис", "hybrid": "гибрид", "remote": "удалённо", "unknown": ""}
EXPERIENCE_TITLES = {
    "none": "Опыт не нужен",
    "1-3": "Опыт 1–3 года",
    "3-6": "Опыт 3–6 лет",
    "6+": "Опыт от 6 лет",
    "unknown": "Опыт не указан",
}


def fmt_money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def render_salary(v: Vacancy) -> str:
    if v.salary_period == "piece":
        return f"сдельно: {v.salary_text.strip()}" if v.salary_text.strip() else "сдельная оплата"
    lo, hi = v.salary_from, v.salary_to
    if lo and hi and lo != hi:
        s = f"{fmt_money(lo)} – {fmt_money(hi)} ₽"
    elif lo:
        s = f"от {fmt_money(lo)} ₽"
    elif hi:
        s = f"до {fmt_money(hi)} ₽"
    else:
        return "зарплата не указана"
    if v.salary_period == "hour":
        s += " · почасовая, ≈ в месяц"
    elif v.salary_period == "shift":
        s += " · за смены, ≈ в месяц"
    if v.salary_net is True:
        s += " · на руки"
    elif v.salary_net is False:
        s += " · до вычета"
    return s


def render_age(published: datetime | None, now: datetime | None = None) -> str:
    if published is None:
        return "дата не указана"
    now = now or datetime.now(UTC)
    pub = published if published.tzinfo else published.replace(tzinfo=UTC)
    days = (now.date() - pub.date()).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 5:
        return f"{days} дня назад"
    return f"{days} дней назад"


def render_card(v: Vacancy, *, now: datetime | None = None, html: bool = True) -> str:
    e = escape if html else (lambda s: s)
    company = e(v.company) or "компания не указана"
    place = " · ".join(x for x in (company, e(v.city), FORMAT_TITLES[v.work_format]) if x)
    exp = EXPERIENCE_TITLES[v.experience] + (" · обучение" if v.training else "")
    links = v.all_links()
    sources = ", ".join(dict.fromkeys(SOURCE_TITLES.get(lk.source, lk.source) for lk in links))
    lines = []
    if v.warnings:
        lines.append("⚠️ " + e("Проверьте внимательно: " + ", ".join(v.warnings)))
    lines += [
        f"💰 {e(render_salary(v))}",
        f"📌 <b>{e(v.title)}</b>" if html else f"📌 {v.title}",
        f"🏢 {place}",
        f"🎓 {exp}",
        f"🕒 {render_age(v.published_at, now)} · {e(sources)}",
    ]
    if html:
        if len(links) == 1:
            lines.append(f'🔗 <a href="{escape(links[0].url, quote=True)}">Открыть вакансию</a>')
        else:
            parts = [
                f'<a href="{escape(lk.url, quote=True)}">'
                f"{escape(SOURCE_TITLES.get(lk.source, lk.source))}</a>"
                for lk in links
            ]
            lines.append("🔗 " + " · ".join(parts))
    else:
        lines += [f"🔗 {lk.url}" for lk in links]
    return "\n".join(lines)


def render_summary(title: str, total: int, shown: int, freelance: int = 0) -> str:
    s = f"<b>{escape(title)}</b>\nНайдено новых: {total}."
    if total > shown:
        s += f" Лучшие {shown} ниже."
    if freelance:
        s += f"\nОтдельным блоком — фриланс и сдельная оплата: {freelance}."
    return s
