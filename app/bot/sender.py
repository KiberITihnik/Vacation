"""Отправка подборок в Telegram с соблюдением лимитов (≤1 сообщение/сек в чат)."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from app.bot.keyboards import vacancy_keyboard
from app.bot.render import render_card, render_summary
from app.config import ProfileConfig
from app.models import Vacancy

log = logging.getLogger(__name__)

PER_CHAT_INTERVAL = 1.1
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class TelegramSender:
    def __init__(self, bot: Bot, summary_if_more_than: int = 5) -> None:
        self.bot = bot
        self.summary_if_more_than = summary_if_more_than
        self._last: dict[int, float] = {}
        self._global = asyncio.Semaphore(25)  # < 30 сообщений/сек на бота

    async def send_text(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> int | None:
        for _ in range(5):
            wait = PER_CHAT_INTERVAL - (time.monotonic() - self._last.get(chat_id, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._global:
                    msg = await self.bot.send_message(
                        chat_id,
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                        link_preview_options=NO_PREVIEW,
                    )
                self._last[chat_id] = time.monotonic()
                return msg.message_id
            except TelegramRetryAfter as exc:
                log.warning("Telegram RetryAfter %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 0.5)
        log.error("Не удалось отправить сообщение в %s", chat_id)
        return None

    async def send_digest(
        self,
        chat_id: int,
        profile: ProfileConfig,
        main: list[Vacancy],
        freelance: list[Vacancy],
        total_new: int,
    ) -> list[tuple[Vacancy, int | None]]:
        sent: list[tuple[Vacancy, int | None]] = []
        shown = len(main) + len(freelance)
        if shown > self.summary_if_more_than:
            await self.send_text(
                chat_id, render_summary(profile.title, total_new, shown, len(freelance))
            )
        for v in main:
            sent.append((v, await self._card(chat_id, v)))
        if freelance:
            await self.send_text(chat_id, "<b>📝 Фриланс и сдельная оплата</b>")
            for v in freelance:
                sent.append((v, await self._card(chat_id, v)))
        return sent

    async def _card(self, chat_id: int, v: Vacancy) -> int | None:
        markup = vacancy_keyboard(v.db_id) if v.db_id else None
        try:
            return await self.send_text(chat_id, render_card(v), markup)
        except TelegramAPIError:
            log.exception("Ошибка отправки карточки %s", v.url)
            return None
