"""Публичные Telegram-каналы с вакансиями через веб-превью https://t.me/s/<канал>.

Аккаунт и Telethon не нужны: это открытая страница, которую Telegram сам отдаёт для
просмотра канала в браузере. Читаем последние посты, отбрасываем рекламу, резюме и
пустышки, из текста достаём должность, зарплату, формат, опыт и компанию.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from app.config import AppConfig, ProfileConfig, SourceConfig
from app.models import Link, Vacancy
from app.processing.normalize import (
    detect_net,
    detect_training,
    norm_text,
    parse_experience,
    parse_salary,
    parse_work_format,
)
from app.sources.base import SourceError
from app.sources.http import PoliteClient

log = logging.getLogger(__name__)

NAME = "telegram_channels"
BASE = "https://t.me/s"

# --------------------------------------------------------------------- разбор HTML


@dataclass(frozen=True)
class Post:
    channel: str
    post_id: int
    text: str
    published_at: datetime | None
    links: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel}/{self.post_id}"


def _node_text(node: Node) -> str:
    html = node.html or ""
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(p|div|blockquote|li)>", "\n", html, flags=re.I)
    text = HTMLParser(html).text(separator="")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_channel_page(html: str, channel: str) -> list[Post]:
    tree = HTMLParser(html)
    posts: list[Post] = []
    for msg in tree.css("div.tgme_widget_message[data-post]"):
        data_post = msg.attributes.get("data-post") or ""
        m = re.search(r"/(\d+)$", data_post)
        text_node = msg.css_first(".tgme_widget_message_text")
        if not m or text_node is None:
            continue
        published = None
        t = msg.css_first("time[datetime]")
        if t is not None:
            try:
                published = datetime.fromisoformat(t.attributes.get("datetime") or "")
            except ValueError:
                published = None
        links = tuple(
            href
            for a in text_node.css("a[href]")
            if (href := a.attributes.get("href")) and href.startswith("http")
        )
        posts.append(
            Post(
                channel=data_post.split("/")[0] or channel,
                post_id=int(m.group(1)),
                text=_node_text(text_node),
                published_at=published,
                links=links,
            )
        )
    return posts


# --------------------------------------------------------------------- разбор поста

_AD_RE = re.compile(r"#реклам|\berid\b|на правах рекламы|^реклама\b|#промо|#partner", re.I | re.M)
_RESUME_RE = re.compile(
    r"#резюме|#ищу_?работу|#ищуработу|#cv\b|^\s*ищу работу|^\s*резюме\b|#соискател", re.I | re.M
)
_VACANCY_RE = re.compile(
    r"ваканси|требуется|требуются|ищем|ищу (?:копирайтер|автор|редактор|smm|ассистент|менеджер)"
    r"|в команду|обязанности|задачи|условия|зарплат|оплата|откликнуться|отклик|#работа|hiring",
    re.I,
)
_CONTACT_RE = re.compile(r"@\w{4,}|https?://|t\.me/|[\w.+-]+@[\w-]+\.\w+|\+7|8 ?\(?9\d\d", re.I)
_ROLE_RE = re.compile(
    r"^(?:[^\wа-яё]*)(?:ваканси[яи]|должность|позиция|роль|ищем|требуется|требуются|position|job)"
    r"\s*[:—–-]?\s*(?P<role>.{3,120})$",
    re.I | re.M,
)
_COMPANY_RE = re.compile(
    r"^(?:[^\wа-яё]*)(?:компания|работодатель|организация|company|бренд|проект)\s*[:—–-]\s*(?P<v>.{2,100})$",
    re.I | re.M,
)
_CITY_RE = re.compile(
    r"^(?:[^\wа-яё]*)(?:город|локация|location|место работы|офис)\s*[:—–-]\s*(?P<v>.{2,80})$",
    re.I | re.M,
)
_SALARY_LINE_RE = re.compile(
    r"зарплат|з/п|\bзп\b|оплата|доход|оклад|ставка|вознагражд|гонорар|бюджет|💰|💵|💸|₽|руб|\$|€|тыс",
    re.I,
)
_EXP_LINE_RE = re.compile(r"опыт", re.I)
_HASHTAG_RE = re.compile(r"#[\wа-яё]+", re.I)
_EMOJI_RE = re.compile(r"[^\w\s.,:;!?()«»\"'+/&%№–—-]", re.UNICODE)


def _clean_line(s: str) -> str:
    s = _HASHTAG_RE.sub(" ", s)
    s = _EMOJI_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—:.,*|")
    return s


def extract_title(text: str) -> str:
    m = _ROLE_RE.search(text)
    if m:
        role = _clean_line(m.group("role"))
        if len(role) >= 3:
            return (role[:1].upper() + role[1:])[:120]
    for line in text.splitlines():
        cleaned = _clean_line(line)
        if len(cleaned) >= 4 and not re.fullmatch(
            r"(?i)ваканси[яи]|новая вакансия|работа", cleaned
        ):
            return cleaned[:120]
    return ""


def _salary_line(text: str) -> str:
    for line in text.splitlines():
        if _SALARY_LINE_RE.search(line) and re.search(r"\d", line):
            return line
    return ""


def _field(regex: re.Pattern[str], text: str) -> str:
    m = regex.search(text)
    return _clean_line(m.group("v")) if m else ""


def is_vacancy_post(text: str, min_length: int) -> str | None:
    """None — пост похож на вакансию, иначе причина отсева."""
    if len(text) < min_length:
        return "short"
    if _AD_RE.search(text):
        return "ad"
    if _RESUME_RE.search(text):
        return "resume"
    if not _VACANCY_RE.search(text):
        return "not_vacancy"
    if not _CONTACT_RE.search(text):
        return "no_contact"
    return None


def post_to_vacancy(post: Post, app: AppConfig, *, assume_remote: bool) -> Vacancy | None:
    text = post.text
    title = extract_title(text)
    if not title:
        return None
    salary_text = _salary_line(text)
    info = parse_salary(salary_text, app.money) if salary_text else None
    exp_line = next((ln for ln in text.splitlines() if _EXP_LINE_RE.search(ln)), "")
    experience = parse_experience(exp_line) if exp_line else "unknown"
    if experience == "unknown" and re.search(
        r"без опыта|опыт не (?:нужен|требуется|важен)", text, re.I
    ):
        experience = "none"
    work_format = parse_work_format(text, default="remote" if assume_remote else "unknown")
    city = _field(_CITY_RE, text)
    if work_format == "remote" and re.search(r"удал|remote|дистанц", city, re.I):
        city = ""
    # одна и та же вакансия часто репостится в несколько каналов — id по тексту
    digest = hashlib.sha1(norm_text(text)[:600].encode()).hexdigest()[:16]
    return Vacancy(
        source=NAME,
        external_id=digest,
        title=title,
        company=_field(_COMPANY_RE, text),
        city=city,
        url=post.url,
        published_at=post.published_at,
        description=text[:2000],
        salary_from=info.salary_from if info else None,
        salary_to=info.salary_to if info else None,
        salary_net=detect_net(salary_text) if salary_text else None,
        salary_period=info.period if info else "unknown",
        salary_text=salary_text[:200],
        work_format=work_format,
        experience=experience,
        training=detect_training(text),
        raw={"channel": post.channel, "post_id": post.post_id, "links": list(post.links)},
        links=[Link(source=NAME, url=post.url, label=f"@{post.channel}")],
    )


# --------------------------------------------------------------------- источник


class TelegramChannelsSource:
    name = NAME

    def __init__(self, app: AppConfig, client: PoliteClient | None = None) -> None:
        self.app = app
        self.cfg: SourceConfig = app.source(NAME)
        self.common: list[str] = list(self.cfg.opt("common_channels", []) or [])
        self.max_age = timedelta(days=float(self.cfg.opt("max_age_days", 2)))
        self.pages = int(self.cfg.opt("pages_per_channel", 1))
        self.min_length = int(self.cfg.opt("min_post_length", 120))
        self.assume_remote = bool(self.cfg.opt("assume_remote", True))
        self.cache_ttl = float(self.cfg.opt("cache_minutes", 20)) * 60
        http = app.http.model_copy(
            update={"delay_seconds": float(self.cfg.opt("delay_seconds", 1.5))}
        )
        self.client = client or PoliteClient(http, headers={"Accept": "text/html"})
        self._cache: dict[str, tuple[float, list[Post]]] = {}
        self._lock = asyncio.Lock()
        self.last_stats: dict[str, Any] = {}

    def channels_for(self, profile: ProfileConfig) -> list[str]:
        seen: dict[str, None] = {}
        for ch in [*self.common, *profile.telegram_channels]:
            name = ch.strip().lstrip("@").removeprefix("https://t.me/").strip("/")
            if name:
                seen.setdefault(name, None)
        return list(seen)

    async def channel_posts(self, channel: str) -> list[Post]:
        async with self._lock:
            cached = self._cache.get(channel)
            if cached and time.monotonic() - cached[0] < self.cache_ttl:
                return cached[1]
            posts: list[Post] = []
            url = f"{BASE}/{channel}"
            for _ in range(self.pages):
                page = parse_channel_page(await self.client.get_text(url), channel)
                if not page:
                    break
                posts.extend(page)
                url = f"{BASE}/{channel}?before={min(p.post_id for p in page)}"
            self._cache[channel] = (time.monotonic(), posts)
            return posts

    async def fetch(self, profile: ProfileConfig) -> list[Vacancy]:
        now = datetime.now(UTC)
        found: dict[str, Vacancy] = {}
        stats: dict[str, Any] = {"channels": 0, "posts": 0, "skipped": {}, "dead": [], "errors": []}
        channels = self.channels_for(profile)
        streak = 0
        for channel in channels:
            try:
                posts = await self.channel_posts(channel)
                streak = 0
            except SourceError as exc:
                stats["errors"].append(f"{channel}: {exc}")
                streak += 1
                if streak >= 5:
                    break  # t.me недоступен целиком — не ждём остальные каналы
                continue
            stats["channels"] += 1
            if not posts:
                stats["dead"].append(channel)
                continue
            for post in posts:
                stats["posts"] += 1
                if post.published_at and now - post.published_at > self.max_age:
                    stats["skipped"]["old"] = stats["skipped"].get("old", 0) + 1
                    continue
                why = is_vacancy_post(post.text, self.min_length)
                vac = (
                    None
                    if why
                    else post_to_vacancy(post, self.app, assume_remote=self.assume_remote)
                )
                if vac is None:
                    key = why or "no_title"
                    stats["skipped"][key] = stats["skipped"].get(key, 0) + 1
                    continue
                if vac.external_id in found:  # репост в другом канале — добавим ссылку
                    found[vac.external_id].links.extend(vac.links)
                else:
                    found[vac.external_id] = vac
        self.last_stats = stats
        if stats["dead"]:
            log.warning(
                "telegram: каналы без постов (нет веб-превью или закрыты): %s", stats["dead"]
            )
        if channels and not stats["channels"]:
            raise SourceError("; ".join(stats["errors"][:3]) or "все каналы недоступны")
        log.info(
            "telegram/%s: каналов %d, постов %d, похожих на вакансии %d, отсев %s",
            profile.id,
            stats["channels"],
            stats["posts"],
            len(found),
            stats["skipped"],
        )
        return list(found.values())

    async def aclose(self) -> None:
        await self.client.aclose()
