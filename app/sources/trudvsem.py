"""«Работа России» (trudvsem.ru) — открытые данные, без авторизации.

Формат ответа (opendata API v1):
    {"status": "200", "meta": {"total": N, "limit": 100},
     "results": {"vacancies": [{"vacancy": {...}}, ...]}}
Параметр offset — номер страницы, limit — размер страницы (≤ 100).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import AppConfig, ProfileConfig, SourceConfig
from app.models import Vacancy
from app.processing.normalize import (
    city_from_address,
    detect_net,
    detect_training,
    parse_experience,
    parse_salary,
    parse_work_format,
    salary_from_numbers,
    strip_html,
)
from app.sources.base import SourceError
from app.sources.http import PoliteClient

log = logging.getLogger(__name__)

NAME = "trudvsem"
DEFAULT_BASE = "http://opendata.trudvsem.ru/api/v1"


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def parse_vacancy(item: dict[str, Any], app: AppConfig) -> Vacancy | None:
    v = item.get("vacancy", item)
    vid = v.get("id")
    title = (v.get("job-name") or "").strip()
    url = v.get("vac_url") or ""
    if not vid or not title:
        return None
    if not url:
        company_code = _dig(v, "company", "companycode") or ""
        url = f"https://trudvsem.ru/vacancy/card/{company_code}/{vid}"

    addresses = _dig(v, "addresses", "address") or []
    first = addresses[0] if isinstance(addresses, list) and addresses else None
    address = str(first.get("location", "")) if isinstance(first, dict) else ""
    region_name = _dig(v, "region", "name") or ""
    city = city_from_address(address) or city_from_address(region_name) or ""
    if not city and "челябинск" in address.lower():
        city = "Челябинск"

    duty = strip_html(v.get("duty"))
    qualification = strip_html(_dig(v, "requirement", "qualification"))
    term = v.get("term")
    conditions = strip_html(term.get("text") if isinstance(term, dict) else "")
    description = " ".join(x for x in (duty, qualification, conditions) if x)

    salary_text = str(v.get("salary") or "")
    lo, hi = salary_from_numbers(v.get("salary_min"), v.get("salary_max"), "rub", app.money)
    period = "month" if (lo or hi) else "unknown"
    if not (lo or hi) and salary_text:
        info = parse_salary(salary_text, app.money)
        lo, hi, period = info.salary_from, info.salary_to, info.period

    schedule = str(v.get("schedule") or "")
    employment = str(v.get("employment") or "")
    work_format = parse_work_format(schedule, employment, title, duty, default="office")

    exp_raw = _dig(v, "requirement", "experience")
    experience = parse_experience(exp_raw if isinstance(exp_raw, int | str) else None)

    published = None
    created = v.get("creation-date") or v.get("modified-date")
    if created:
        try:
            published = datetime.fromisoformat(str(created)).replace(tzinfo=UTC)
        except ValueError:
            published = None

    return Vacancy(
        source=NAME,
        external_id=str(vid),
        title=title,
        company=(_dig(v, "company", "name") or "").strip(),
        city=city or region_name,
        url=url,
        published_at=published,
        description=description[:2000],
        salary_from=lo,
        salary_to=hi,
        salary_net=detect_net(salary_text),
        salary_period=period,  # type: ignore[arg-type]
        salary_text=salary_text,
        work_format=work_format,
        experience=experience,
        training=detect_training(title, description),
        raw=v,
    )


class TrudvsemSource:
    name = NAME

    def __init__(self, app: AppConfig, client: PoliteClient | None = None) -> None:
        self.app = app
        self.cfg: SourceConfig = app.source(NAME)
        self.base = str(self.cfg.opt("base_url", DEFAULT_BASE)).rstrip("/")
        self.page_size = int(self.cfg.opt("page_size", 100))
        self.max_pages = int(self.cfg.opt("max_pages", 3))
        self.modified_days = int(self.cfg.opt("modified_days", 3))
        self.client = client or PoliteClient(app.http)

    def _targets(self, profile: ProfileConfig) -> list[tuple[str, bool]]:
        """(url, remote_only): региональный поиск и/или удалёнка по всей России."""
        targets: list[tuple[str, bool]] = []
        if profile.locations.allow_office:
            for code in profile.trudvsem_regions:
                targets.append((f"{self.base}/vacancies/region/{code}", False))
        if profile.locations.allow_remote:
            targets.append((f"{self.base}/vacancies", True))
        return targets

    async def _search(self, url: str, text: str) -> list[dict[str, Any]]:
        since = (datetime.now(UTC) - timedelta(days=self.modified_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        items: list[dict[str, Any]] = []
        for page in range(self.max_pages):
            params = {"text": text, "offset": page, "limit": self.page_size, "modifiedFrom": since}
            data = await self.client.get_json(url, params=params)
            if not isinstance(data, dict):
                raise SourceError(f"unexpected trudvsem response: {type(data).__name__}")
            batch = _dig(data, "results", "vacancies") or []
            items.extend(batch)
            if len(batch) < self.page_size:
                break
        return items

    async def fetch(self, profile: ProfileConfig) -> list[Vacancy]:
        found: dict[str, Vacancy] = {}
        errors: list[str] = []
        streak = 0
        for url, remote_only in self._targets(profile):
            for query in profile.queries:
                try:
                    raw_items = await self._search(url, query)
                    streak = 0
                except SourceError as exc:
                    errors.append(str(exc))
                    streak += 1
                    if streak >= 3:
                        break  # сайт недоступен — не перебираем остальные запросы
                    continue
                for item in raw_items:
                    try:
                        vac = parse_vacancy(item, self.app)
                    except Exception:  # noqa: BLE001 — формат мог поменяться, не роняем запуск
                        log.exception("trudvsem: cannot parse vacancy")
                        continue
                    if vac is None or (remote_only and vac.work_format != "remote"):
                        continue
                    found.setdefault(vac.external_id, vac)
        if errors and not found:
            raise SourceError("; ".join(errors[:3]))
        if errors:
            log.warning("trudvsem: %d запросов с ошибкой: %s", len(errors), errors[0])
        return list(found.values())

    async def aclose(self) -> None:
        await self.client.aclose()
