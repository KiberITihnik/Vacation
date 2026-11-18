"""Кнопки под карточкой. Обработка нажатий — этап 4."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ACTIONS = {
    "like": "👍 Интересно",
    "dislike": "👎 Не то",
    "applied": "✅ Откликнулась",
    "hide_company": "🚫 Скрыть компанию",
}


def vacancy_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    b = [
        InlineKeyboardButton(text=t, callback_data=f"fb:{a}:{vacancy_id}")
        for a, t in ACTIONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[b[:2], b[2:]])
