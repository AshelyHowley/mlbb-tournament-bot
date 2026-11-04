from __future__ import annotations

import logging

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.bot.context import Services
from app.handlers.common import menu_view

log = logging.getLogger(__name__)
router = Router(name="fallback")


@router.callback_query()
async def stale_callback(cb: CallbackQuery) -> None:
    """Anything not handled above: stale button, wrong state, or non-admin forging admin data."""
    await cb.answer("Кнопка устарела. Откройте меню: /start", show_alert=False)


@router.message()
async def unknown_message(message: Message, svc: Services) -> None:
    text, markup = await menu_view(svc, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.errors()
async def on_error(event: ErrorEvent, state: FSMContext = None) -> bool:
    # Traceback goes to the (redacted) log only - never to the user.
    log.error("Unhandled error in update handler", exc_info=event.exception)
    upd = event.update
    try:
        if state is not None:
            await state.clear()
        if upd.callback_query:
            await upd.callback_query.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
        elif upd.message:
            await upd.message.answer("⚠️ Произошла ошибка. Попробуйте ещё раз: /start")
    except Exception:
        log.debug("could not notify user about error", exc_info=True)
    return True
