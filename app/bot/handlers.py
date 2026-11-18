"""Команды бота. Доступ только для chat_id из .env."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.render import render_card
from app.config import AppConfig, Settings
from app.db import Database
from app.models import utcnow
from app.pipeline import Pipeline

log = logging.getLogger(__name__)

PRIVATE = "Это частный бот."


def profile_for_chat(app: AppConfig, chat_id: int) -> str | None:
    for pid, p in app.profiles.items():
        if p.chat_id == chat_id:
            return pid
    return None


def build_router(app: AppConfig, settings: Settings, db: Database, pipeline: Pipeline) -> Router:
    router = Router()
    admin = settings.admin_chat_id

    def allowed(chat_id: int) -> bool:
        return chat_id == admin or profile_for_chat(app, chat_id) is not None

    @router.message(CommandStart())
    async def start(m: Message) -> None:
        chat_id = m.chat.id
        pid = profile_for_chat(app, chat_id)
        if pid:
            p = app.profiles[pid]
            await m.answer(
                f"Привет! Твой профиль: <b>{escape(pid)}</b> — {escape(p.title)}\n"
                f"Подборки приходят в {', '.join(app.schedule.times)}.\n\n"
                "/now — подборка прямо сейчас\n/top — лучшие за неделю\n"
                "/pause, /resume — пауза рассылки\n/minsalary 60000 — минимальная зарплата"
            )
        elif chat_id == admin:
            await m.answer("Привет, админ. /stats — статистика, /now — запуск по всем профилям.")
        else:
            await m.answer(PRIVATE)
            log.info("/start от постороннего: chat_id=%s", chat_id)
            if admin:
                user = m.from_user
                if user and user.username:
                    who = f"@{user.username}"
                else:
                    who = user.full_name if user else "?"
                await m.bot.send_message(  # type: ignore[union-attr]
                    admin,
                    f"Новый пользователь {escape(who)} написал /start.\n"
                    f"chat_id: <code>{chat_id}</code> — добавьте его в .env, если это свой.",
                )

    @router.message(Command("now"))
    async def now(m: Message) -> None:
        if not allowed(m.chat.id):
            await m.answer(PRIVATE)
            return
        pid = profile_for_chat(app, m.chat.id)
        ids = [pid] if pid else list(app.profiles)
        cooldown = timedelta(minutes=app.schedule.now_cooldown_minutes)
        if m.chat.id != admin:
            last = db.get_state(ids[0]).last_manual_run_at
            if last and utcnow() - last < cooldown:
                left = int((cooldown - (utcnow() - last)).total_seconds() // 60) + 1
                await m.answer(f"Подборку уже запускали недавно. Попробуйте через {left} мин.")
                return
        for i in ids:
            db.update_state(i, last_manual_run_at=utcnow())
        await m.answer("Ищу вакансии, это займёт пару минут… ⏳")

        async def job() -> None:
            for i in ids:
                rep = await pipeline.run_profile(i, manual=True)
                if m.chat.id == admin:
                    await m.answer(
                        f"{escape(i)}: найдено {rep.found}, новых {rep.new}, "
                        f"отправлено {rep.sent}{', ' + rep.skipped if rep.skipped else ''}"
                    )

        asyncio.create_task(job())

    @router.message(Command("pause"))
    async def pause(m: Message) -> None:
        pid = profile_for_chat(app, m.chat.id)
        if not pid:
            await m.answer(PRIVATE)
            return
        db.update_state(pid, paused=True)
        await m.answer("Рассылка на паузе. /resume — возобновить.")

    @router.message(Command("resume"))
    async def resume(m: Message) -> None:
        pid = profile_for_chat(app, m.chat.id)
        if not pid:
            await m.answer(PRIVATE)
            return
        db.update_state(pid, paused=False)
        await m.answer("Рассылка возобновлена ✅")

    @router.message(Command("minsalary"))
    async def minsalary(m: Message, command: CommandObject) -> None:
        pid = profile_for_chat(app, m.chat.id)
        if not pid:
            await m.answer(PRIVATE)
            return
        arg = (command.args or "").replace(" ", "").replace(" ", "")
        if not arg.isdigit():
            cur = db.get_state(pid).min_salary or app.profiles[pid].min_salary
            await m.answer(f"Сейчас минимальная зарплата: {cur} ₽.\nПример: /minsalary 60000")
            return
        db.update_state(pid, min_salary=int(arg))
        await m.answer(f"Готово: показываю вакансии от {int(arg)} ₽ (и без указанной зарплаты).")

    @router.message(Command("top"))
    async def top(m: Message) -> None:
        pid = profile_for_chat(app, m.chat.id)
        if not pid:
            await m.answer(PRIVATE)
            return
        items = db.top_delivered(pid, days=7, limit=10)
        if not items:
            await m.answer("За последние 7 дней ничего не отправлялось.")
            return
        await m.answer("<b>Лучшие за 7 дней</b>")
        for v in items:
            await m.answer(render_card(v), disable_web_page_preview=True)
            await asyncio.sleep(1.1)

    @router.message(Command("stats"))
    async def stats(m: Message) -> None:
        if m.chat.id != admin:
            await m.answer(PRIVATE)
            return
        rows = db.stats(days=7)
        if not rows:
            await m.answer("Запусков за 7 дней нет.")
            return
        lines = ["<b>За 7 дней</b> (профиль / источник: запусков, найдено, отправлено, ошибок)"]
        for pid, src, runs, found, sent, errs in sorted(rows):
            lines.append(f"{escape(pid)} / {escape(src)}: {runs}, {found}, {sent}, {errs}")
        await m.answer("\n".join(lines))

    @router.callback_query(F.data.startswith("fb:"))
    async def feedback(cq: CallbackQuery) -> None:
        chat_id = cq.message.chat.id if cq.message else cq.from_user.id
        pid = profile_for_chat(app, chat_id)
        try:
            _, action, vid = (cq.data or "").split(":")
            vacancy_id = int(vid)
        except ValueError:
            await cq.answer()
            return
        if not pid:
            await cq.answer(PRIVATE)
            return
        db.add_feedback(pid, vacancy_id, action)
        answers = {
            "like": "Запомнила 👍",
            "dislike": "Похожие будут ниже в списке",
            "applied": "Удачи! ✅",
            "hide_company": "Эта компания больше не появится",
        }
        await cq.answer(answers.get(action, "Ок"))

    @router.message()
    async def fallback(m: Message) -> None:
        if not allowed(m.chat.id):
            await m.answer(PRIVATE)
            return
        await m.answer("Команды: /now, /top, /pause, /resume, /minsalary 60000")

    return router
