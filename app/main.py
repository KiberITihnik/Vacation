"""Точка входа: Telegram-бот + планировщик подборок."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.bot.handlers import build_router
from app.bot.sender import TelegramSender
from app.config import AppConfig, Settings, get_config, get_settings
from app.db import Database
from app.logging_setup import setup_logging
from app.pipeline import Pipeline
from app.sources import build_sources

log = logging.getLogger(__name__)


def build_bot_and_sender(settings: Settings, app: AppConfig) -> tuple[Bot, TelegramSender]:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, TelegramSender(bot, app.schedule.summary_if_more_than)


def build_scheduler(settings: Settings, app: AppConfig, pipeline: Pipeline) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    for t in app.schedule.times:
        hour, minute = (int(x) for x in t.split(":"))
        scheduler.add_job(
            pipeline.run_all,
            CronTrigger(hour=hour, minute=minute, timezone=settings.tz),
            id=f"digest-{t}",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    return scheduler


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_dir)
    app = get_config()
    db = Database(settings.database_url)
    bot, sender = build_bot_and_sender(settings, app)
    pipeline = Pipeline(app, settings, db, build_sources(app), sender)

    dp = Dispatcher()
    dp.include_router(build_router(app, settings, db, pipeline))
    scheduler = build_scheduler(settings, app, pipeline)
    scheduler.start()
    log.info(
        "Бот запущен. Профили: %s. Расписание: %s (%s). Источники: %s",
        ", ".join(app.profiles),
        ", ".join(app.schedule.times),
        settings.tz,
        ", ".join(pipeline.sources) or "нет",
    )
    if settings.admin_chat_id:
        try:
            await sender.send_text(settings.admin_chat_id, "🤖 Бот запущен")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось написать админу — проверьте ADMIN_CHAT_ID")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await pipeline.aclose()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
