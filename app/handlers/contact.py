"""Personal messages between staff and team captains, through the bot.

Staff pick a captain in «Рассылка» → «Написать капитану лично» (admin_broadcast.py). The captain gets
the message with a «Ответить организатору» button; the reply flow below is open to any user, but the
reply only ever reaches someone who is staff right now (re-checked server-side), so a forged
callback cannot turn the bot into a relay to arbitrary users."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.context import Services
from app.handlers.common import show
from app.keyboards import admin as akb
from app.keyboards import common as ckb
from app.keyboards.common import B, kb
from app.repositories import teams as teams_repo
from app.services.broadcast import Payload, send_payload, validate_payload
from app.services.errors import ServiceError
from app.utils.text import esc, parse_int, person

log = logging.getLogger(__name__)
router = Router(name="contact")


class CaptainReply(StatesGroup):
    content = State()


def extract_payload(message: Message) -> Optional[Payload]:
    if message.photo:
        return Payload("photo", message.caption, message.photo[-1].file_id)
    if message.video:
        return Payload("video", message.caption, message.video.file_id)
    if message.document:
        return Payload("document", message.caption, message.document.file_id)
    if message.text:
        return Payload("text", message.text)
    return None


async def deliver_personal(bot, chat_id: int, header: str, payload: Payload, markup) -> bool:
    """Header (HTML, ours) + the author's content verbatim, with the reply button under the content."""
    try:
        await bot.send_message(chat_id, header)
        await send_payload(bot, chat_id, payload, reply_markup=markup)
        return True
    except TelegramAPIError as e:
        log.info("personal message to %s not delivered: %s", chat_id, type(e).__name__)
        return False


def _reply_cancel():
    return kb([[B(text="❌ Отмена", callback_data="dmr:cancel")]])


@router.callback_query(F.data.regexp(r"^dmr:\d{1,18}$"))
async def reply_start(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    staff_id = parse_int(cb.data.split(":")[1])
    if staff_id is None or not await svc.roles.is_staff(staff_id):
        staff_id = svc.cfg.admin_id  # the sender is no longer staff: the main admin gets the reply
    await state.clear()
    await state.set_state(CaptainReply.content)
    await state.update_data(reply_to=staff_id)
    await cb.message.answer(
        "✉️ <b>Ответ организатору</b>\n\nОтправьте сообщение: текст, фото, видео или документ.",
        reply_markup=_reply_cancel(),
    )


@router.callback_query(F.data == "dmr:cancel")
async def reply_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    await state.clear()
    await show(cb, "❌ Ответ отменён.", ckb.back_to_menu())


@router.message(CaptainReply.content)
async def reply_content(message: Message, state: FSMContext, svc: Services) -> None:
    payload = extract_payload(message)
    if payload is None:
        await message.answer("❌ Поддерживаются текст, фото, видео и документ.", reply_markup=_reply_cancel())
        return
    try:
        validate_payload(payload)
    except ServiceError as e:
        await message.answer(e.user_message, reply_markup=_reply_cancel())
        return
    target = (await state.get_data()).get("reply_to") or svc.cfg.admin_id
    await state.clear()
    u = message.from_user
    async with svc.team.pool.acquire() as conn:
        team = await teams_repo.captain_team(conn, u.id)
    where = f"капитан команды «{esc(team['name'])}»" if team else "участник"
    header = f"✉️ <b>Ответ от {person(u.full_name, u.username, u.id)}</b> ({where}):"
    ok = await deliver_personal(svc.bot, target, header, payload, akb.dm_answer(u.id) if team else None)
    await message.answer("✅ Ответ отправлен организатору." if ok else "⚠️ Не удалось доставить ответ, попробуйте позже.",
                         reply_markup=ckb.back_to_menu())
