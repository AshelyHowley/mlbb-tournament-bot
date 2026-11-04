from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.context import Services
from app.handlers.common import IsStaff, show
from app.keyboards import admin as akb
from app.services.broadcast import AUDIENCES, Payload, send_payload
from app.services.errors import ServiceError
from app.utils.text import esc, parse_int

log = logging.getLogger(__name__)
router = Router(name="admin_broadcast")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

TYPE_TITLES = {"text": "текст", "photo": "фото", "video": "видео"}


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


@router.callback_query(F.data == "adm:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await show(cb, "📢 <b>Рассылка</b>\nКому отправить?", akb.bc_audience())


@router.callback_query(F.data.regexp(r"^bc:aud:(captains|solo|all)$"))
async def bc_audience(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    aud = cb.data.split(":")[2]
    await state.clear()
    await state.set_state(Broadcast.content)
    await state.update_data(audience=aud)
    await show(
        cb,
        f"Аудитория: <b>{esc(AUDIENCES[aud])}</b>\n\nОтправьте сообщение для рассылки: <b>текст</b>, <b>фото</b> или <b>видео</b> (с подписью). "
        "Текст отправляется как есть, без форматирования.",
        akb.bc_cancel(),
    )


@router.callback_query(F.data == "bc:abort")
async def bc_abort(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    await state.clear()
    await show(cb, "🛠 <b>Админ-панель</b>", akb.admin_menu())


def _extract(message: Message) -> Optional[Payload]:
    if message.photo:
        return Payload("photo", message.caption, message.photo[-1].file_id)
    if message.video:
        return Payload("video", message.caption, message.video.file_id)
    if message.text:
        return Payload("text", message.text)
    return None


@router.message(Broadcast.content)
async def bc_content(message: Message, state: FSMContext, svc: Services) -> None:
    payload = _extract(message)
    if payload is None:
        await message.answer("❌ Поддерживаются только текст, фото и видео. Отправьте другое сообщение.", reply_markup=akb.bc_cancel())
        return
    data = await state.get_data()
    aud = data["audience"]
    try:
        bid, count = await svc.bc.prepare(message.from_user.id, aud, payload)
    except ServiceError as e:
        await message.answer(e.user_message, reply_markup=akb.bc_cancel())
        return
    if count == 0:
        await svc.bc.cancel(bid)
        await message.answer(
            "⚠️ В выбранной аудитории нет доступных получателей. Выберите другую аудиторию:", reply_markup=akb.bc_audience()
        )
        await state.clear()
        return
    await state.set_state(Broadcast.confirm)
    await state.update_data(bid=bid)
    await message.answer(
        f"📋 <b>Предпросмотр рассылки</b>\n\nТип: <b>{TYPE_TITLES[payload.content_type]}</b>\n"
        f"Получатели: <b>{esc(AUDIENCES[aud])}</b>\nКоличество: <b>{count}</b>\n\nСообщение будет выглядеть так:"
    )
    try:
        await send_payload(message.bot, message.from_user.id, payload)
    except Exception:
        log.exception("preview failed")
        await message.answer("⚠️ Не удалось показать предпросмотр.")
    await message.answer("Отправить?", reply_markup=akb.bc_confirm(bid))


async def _drop_draft(svc: Services, bid: Optional[int]) -> None:
    if bid:
        await svc.bc.cancel(bid)


@router.callback_query(Broadcast.confirm, F.data.regexp(r"^bc:edit:\d{1,18}$"))
async def bc_edit(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    bid = parse_int(cb.data.split(":")[2])
    if bid != (await state.get_data()).get("bid"):
        return
    await _drop_draft(svc, bid)
    await state.set_state(Broadcast.content)
    await show(cb, "✏️ Отправьте новое сообщение для рассылки (текст, фото или видео):", akb.bc_cancel())


@router.callback_query(Broadcast.confirm, F.data.regexp(r"^bc:cancel:\d{1,18}$"))
async def bc_cancel(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer("Отменено")
    await _drop_draft(svc, parse_int(cb.data.split(":")[2]))
    await state.clear()
    await show(cb, "❌ Рассылка отменена.", akb.admin_menu())


@router.callback_query(Broadcast.confirm, F.data.regexp(r"^bc:send:\d{1,18}$"))
async def bc_send(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    bid = parse_int(cb.data.split(":")[2])
    if bid != (await state.get_data()).get("bid"):
        return
    await state.clear()  # second click finds no state; the DB claim is the second line of defence
    try:
        total = await svc.bc.start(bid, cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.admin_menu())
        return
    await show(cb, f"📤 Рассылка #{bid} запущена. Получателей: <b>{total}</b>.\nПо завершении пришлю отчёт.", akb.admin_menu())
