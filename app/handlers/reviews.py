"""Reviews — users can leave and read reviews."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.context import Services
from app.handlers.common import show
from app.keyboards import common as kbs
from app.utils.text import esc, person

log = logging.getLogger(__name__)
router = Router(name="reviews")


class ReviewForm(StatesGroup):
    text = State()


@router.callback_query(F.data == "reviews")
async def reviews_menu(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, full_name, text, created_at FROM reviews ORDER BY id DESC LIMIT 10"
        )
    if not rows:
        text = "⭐ <b>Отзывы</b>\n\nПока отзывов нет. Будьте первым!"
    else:
        lines = ["⭐ <b>Отзывы</b>\n"]
        for r in rows:
            who = person(r["full_name"], r["username"], r["user_id"])
            lines.append(f"👤 {who}\n{esc(r['text'])}\n")
        text = "\n".join(lines)
    await show(cb, text, kbs.reviews_menu())


@router.callback_query(F.data == "reviews:write")
async def reviews_write(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(ReviewForm.text)
    await show(cb, "✍️ Напишите ваш отзыв (до 500 символов):", kbs.nav())


@router.message(ReviewForm.text)
async def reviews_save(message: Message, state: FSMContext, svc: Services) -> None:
    text = (message.text or "").strip()
    if not text or len(text) < 5:
        await message.answer("Отзыв слишком короткий. Минимум 5 символов.")
        return
    if len(text) > 500:
        await message.answer("Отзыв слишком длинный. Максимум 500 символов.")
        return
    user = message.from_user
    async with svc.team.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reviews (user_id, username, full_name, text) VALUES ($1, $2, $3, $4)",
            user.id, user.username, user.full_name, text,
        )
    await state.clear()
    await message.answer("✅ Спасибо! Ваш отзыв сохранён.", reply_markup=kbs.back_to_menu())



@router.callback_query(ReviewForm.text, F.data == "reg:back")
@router.callback_query(ReviewForm.text, F.data == "reg:cancel")
async def reviews_cancel(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await state.clear()
    await reviews_menu(cb, svc)

@router.callback_query(F.data == "reviews:back")
async def reviews_back(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await state.clear()
    await reviews_menu(cb, svc)
