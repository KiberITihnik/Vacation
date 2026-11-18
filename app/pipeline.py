"""Конвейер: collect → normalize → dedupe → filter → score → send.

Проверка без отправки:  python -m app.pipeline --dry-run --profile sister
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.bot.render import render_card
from app.config import AppConfig, ProfileConfig, Settings, get_config, get_settings
from app.db import Database
from app.models import RunRow, Vacancy
from app.processing.antiscam import AntiScam
from app.processing.dedupe import dedupe, fingerprint
from app.processing.filters import ProfileFilter
from app.processing.scoring import rank
from app.sources import build_sources
from app.sources.base import Source

log = logging.getLogger(__name__)

UNHEALTHY_RUNS = 3


class DigestSender(Protocol):
    async def send_digest(
        self,
        chat_id: int,
        profile: ProfileConfig,
        main: list[Vacancy],
        freelance: list[Vacancy],
        total_new: int,
    ) -> list[tuple[Vacancy, int | None]]: ...

    async def send_text(self, chat_id: int, text: str) -> int | None: ...


@dataclass
class ProfileReport:
    profile_id: str
    found: int = 0
    new: int = 0
    hidden_scam: int = 0
    main: list[Vacancy] = field(default_factory=list)
    freelance: list[Vacancy] = field(default_factory=list)
    sent: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    skipped: str | None = None


class Pipeline:
    def __init__(
        self,
        app: AppConfig,
        settings: Settings,
        db: Database,
        sources: dict[str, Source],
        sender: DigestSender | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.db = db
        self.sources = sources
        self.sender = sender
        self.antiscam = AntiScam(app.antiscam)
        self._locks: dict[str, asyncio.Lock] = {}
        # источники со свободным текстом: ключевые слова ищем и в начале описания
        self.text_sources = frozenset(
            name for name, cfg in app.sources.items() if cfg.opt("keywords_in_text", False)
        )
        self.source_caps: dict[str, int] = {
            name: int(cfg.opt("max_in_digest"))
            for name, cfg in app.sources.items()
            if cfg.opt("max_in_digest") is not None
        }

    async def _admin(self, text: str) -> None:
        log.warning("ADMIN: %s", text)
        if self.sender and self.settings.admin_chat_id:
            try:
                await self.sender.send_text(self.settings.admin_chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось написать админу")

    async def collect(
        self, profile: ProfileConfig, dry_run: bool
    ) -> tuple[list[Vacancy], dict[str, str]]:
        vacancies: list[Vacancy] = []
        errors: dict[str, str] = {}
        for name in profile.sources:
            source = self.sources.get(name)
            if source is None:
                continue
            err: str | None = None
            batch: list[Vacancy] = []
            try:
                batch = await source.fetch(profile)
            except Exception as exc:  # noqa: BLE001 — один источник не роняет запуск
                log.exception("Источник %s упал", name)
                err = f"{type(exc).__name__}: {exc}"[:1000]
                errors[name] = err
            vacancies.extend(batch)
            log.info("%s/%s: %d вакансий", profile.id, name, len(batch))
            if dry_run:
                continue
            self.db.add_run(RunRow(profile_id=profile.id, source=name, found=len(batch), error=err))
            if err:
                await self._admin(f"❗ Источник {name} ({profile.id}): {err}")
            recent = self.db.last_runs(profile.id, name, UNHEALTHY_RUNS)
            if len(recent) == UNHEALTHY_RUNS and all(r.error or r.found == 0 for r in recent):
                await self._admin(
                    f"⚠️ {name} ({profile.id}): {UNHEALTHY_RUNS} запуска подряд 0 вакансий "
                    "или ошибка — возможно, изменился формат"
                )
        return vacancies, errors

    def select(
        self, profile: ProfileConfig, vacancies: list[Vacancy], report: ProfileReport
    ) -> None:
        """Всё, кроме сети и отправки: удобно тестировать."""
        state = self.db.get_state(profile.id)
        for v in vacancies:
            v.db_id = self.db.upsert_vacancy(v, fingerprint(v))
        unique = dedupe(vacancies)
        sent_ids, sent_fps = self.db.delivered(profile.id)
        fresh = [v for v in unique if v.db_id not in sent_ids and fingerprint(v) not in sent_fps]
        report.new = len(fresh)
        kept, hidden = self.antiscam.apply(fresh)
        report.hidden_scam = len(hidden)
        flt = ProfileFilter(
            profile,
            min_salary=state.min_salary,
            hidden_companies=self.db.feedback_companies(profile.id),
            text_sources=self.text_sources,
        )
        res = flt.apply(kept)
        report.rejected = dict(res.rejected)
        disliked = self.db.feedback_titles(profile.id)
        ts = self.text_sources
        sc = self.app.scoring
        main = self._cap(rank(res.kept, profile, sc, disliked_titles=disliked, text_sources=ts))
        free = self._cap(
            rank(res.freelance, profile, sc, disliked_titles=disliked, text_sources=ts)
        )
        limit = self.app.schedule.max_per_digest
        free_cap = min(len(free), max(limit // 3, limit - len(main))) if free else 0
        report.freelance = free[:free_cap]
        report.main = main[: limit - len(report.freelance)]

    def _cap(self, ranked: list[Vacancy]) -> list[Vacancy]:
        """Не больше max_in_digest карточек из одного источника (например, Telegram)."""
        if not self.source_caps:
            return ranked
        used: dict[str, int] = {}
        out = []
        for v in ranked:
            cap = self.source_caps.get(v.source)
            if cap is not None and used.get(v.source, 0) >= cap:
                continue
            used[v.source] = used.get(v.source, 0) + 1
            out.append(v)
        return out

    async def run_profile(
        self, profile_id: str, *, dry_run: bool = False, manual: bool = False
    ) -> ProfileReport:
        profile = self.app.profiles[profile_id]
        report = ProfileReport(profile_id)
        lock = self._locks.setdefault(profile_id, asyncio.Lock())
        if lock.locked():
            report.skipped = "already_running"
            return report
        async with lock:
            if not dry_run and not manual and self.db.get_state(profile_id).paused:
                report.skipped = "paused"
                return report
            chat_id = profile.chat_id
            if not dry_run and (chat_id is None or self.sender is None):
                report.skipped = "no_chat_id"
                log.warning("Профиль %s: не задан %s", profile_id, profile.chat_id_env)
                return report

            vacancies, report.errors = await self.collect(profile, dry_run)
            report.found = len(vacancies)
            self.select(profile, vacancies, report)

            if dry_run:
                return report
            assert chat_id is not None and self.sender is not None
            total = len(report.main) + len(report.freelance)
            if total:
                sent = await self.sender.send_digest(
                    chat_id, profile, report.main, report.freelance, report.new
                )
                for v, msg_id in sent:
                    if msg_id is not None and v.db_id is not None:
                        self.db.record_delivery(
                            profile_id, v.db_id, fingerprint(v), msg_id, v.score
                        )
                        report.sent += 1
            elif self.app.schedule.send_empty_notice:
                await self.sender.send_text(chat_id, "Новых подходящих вакансий нет 🙂")
            self.db.add_run(
                RunRow(
                    profile_id=profile_id,
                    source="*",
                    found=report.found,
                    filtered=total,
                    sent=report.sent,
                    error="; ".join(f"{k}: {v}" for k, v in report.errors.items()) or None,
                )
            )
            return report

    async def run_all(self, *, manual: bool = False) -> list[ProfileReport]:
        return [await self.run_profile(pid, manual=manual) for pid in self.app.profiles]

    async def aclose(self) -> None:
        for s in self.sources.values():
            await s.aclose()


def print_report(report: ProfileReport, out: Callable[[str], object] = print) -> None:
    out(
        f"=== {report.profile_id}: найдено {report.found}, новых {report.new}, "
        f"скрыто антискамом {report.hidden_scam}, отсев {report.rejected}"
    )
    for name, err in report.errors.items():
        out(f"!!! {name}: {err}")
    for i, v in enumerate(report.main, 1):
        out(f"\n--- #{i} (score {v.score})\n{render_card(v, html=False)}")
    if report.freelance:
        out("\n=== Фриланс и сдельная оплата")
        for v in report.freelance:
            out(f"\n{render_card(v, html=False)}")


async def _cli(profile: str | None, dry_run: bool) -> None:
    from app.logging_setup import setup_logging

    settings = get_settings()
    setup_logging(settings.log_dir)
    app = get_config()
    db = Database(settings.database_url)
    pipeline = Pipeline(app, settings, db, build_sources(app))
    try:
        ids = [profile] if profile else list(app.profiles)
        if dry_run:
            for pid in ids:
                print_report(await pipeline.run_profile(pid, dry_run=True))
        else:
            from app.main import build_bot_and_sender

            bot, sender = build_bot_and_sender(settings, app)
            pipeline.sender = sender
            try:
                for pid in ids:
                    print_report(await pipeline.run_profile(pid, manual=True))
            finally:
                await bot.session.close()
    finally:
        await pipeline.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description="Разовый запуск подборки")
    ap.add_argument("--profile", help="id профиля из profiles.yaml (по умолчанию все)")
    ap.add_argument("--dry-run", action="store_true", help="напечатать, ничего не отправляя")
    args = ap.parse_args()
    asyncio.run(_cli(args.profile, args.dry_run))


if __name__ == "__main__":
    main()
