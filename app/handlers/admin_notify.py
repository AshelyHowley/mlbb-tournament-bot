"""Targeted staff notifications: a custom series win/loss message, or the tournament's final
placement broadcast. Reuses the same draft -> preview -> confirm -> send shape as the general
broadcast, on its own small table, so the well-tested general broadcast is never touched."""
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
from app.services.broadcast import Payload, send_payload
from app.services.errors import ServiceError
from app.services.result_notify import KINDS
from app.utils.text import esc, parse_int

log = logging.getLogger(__name__)
router = Router(name="admin_notify")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

TYPE_TITLES = {"text": "текст", "photo": "фото", "video": "видео", "document": "документ"}


class Notify(StatesGroup):
    content = State()
    confirm = State()


@router.callback_query(F.data.regexp(r"^sn:pick:\d{1,18}$"))
async def pick_series(cb: CallbackQuery) -> None:
    await cb.answer()
    mid = parse_int(cb.data.split(":")[2])
    await show(cb, "📨 <b>Уведомления по матчу</b>\n\nКому отправить?", akb.sn_audience_series(mid))


@router.callback_query(F.data.regexp(r"^sn:aud:(series_winners|series_losers|final_first|final_second|final_third|final_others|final_all):\d{1,18}$"))
async def pick_audience(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    _, _, kind, context_id = cb.data.split(":")
    await state.clear()
    await state.set_state(Notify.content)
    await state.update_data(kind=kind, context_id=int(context_id))
    await show(
        cb,
        f"Аудитория: <b>{esc(KINDS[kind])}</b>\n\nОтправьте сообщение: текст, фото, видео или документ (с подписью).",
        akb.sn_cancel(),
    )


@router.callback_query(F.data == "sn:abort")
async def abort(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    await state.clear()
    await show(cb, "🛠 <b>Админ-панель</b>", akb.admin_menu())


def _extract(message: Message) -> Optional[Payload]:
    if message.photo:
        return Payload("photo", message.caption, message.photo[-1].file_id)
    if message.video:
        return Payload("video", message.caption, message.video.file_id)
    if message.document:
        return Payload("document", message.caption, message.document.file_id)
    if message.text:
        return Payload("text", message.text)
    return None


@router.message(Notify.content)
async def content(message: Message, state: FSMContext, svc: Services) -> None:
    payload = _extract(message)
    if payload is None:
        await message.answer("❌ Поддерживаются текст, фото, видео и документ. Отправьте другое сообщение.", reply_markup=akb.sn_cancel())
        return
    data = await state.get_data()
    try:
        rid, count = await svc.result_notify.prepare(message.from_user.id, data["kind"], data["context_id"], payload)
    except ServiceError as e:
        await message.answer(e.user_message, reply_markup=akb.sn_cancel())
        return
    if count == 0:
        await svc.result_notify.cancel(rid)
        await message.answer("⚠️ В выбранной аудитории нет получателей.", reply_markup=akb.admin_menu())
        await state.clear()
        return
    await state.set_state(Notify.confirm)
    await state.update_data(rid=rid)
    await message.answer(
        f"👁 <b>ПРЕДПРОСМОТР</b>\n\nАудитория: <b>{esc(KINDS[data['kind']])}</b>\nПолучателей: <b>{count}</b>\n\n"
        f"Тип: <b>{TYPE_TITLES[payload.content_type]}</b>\n\nСообщение будет выглядеть так:"
    )
    try:
        await send_payload(message.bot, message.from_user.id, payload)
    except Exception:
        log.exception("preview failed")
        await message.answer("⚠️ Не удалось показать предпросмотр.")
    await message.answer("Отправить?", reply_markup=akb.sn_confirm(rid))


async def _drop(svc: Services, rid: Optional[int]) -> None:
    if rid:
        await svc.result_notify.cancel(rid)


@router.callback_query(Notify.confirm, F.data.regexp(r"^sn:edit:\d{1,18}$"))
async def edit(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    rid = parse_int(cb.data.split(":")[2])
    if rid != (await state.get_data()).get("rid"):
        return
    await _drop(svc, rid)
    await state.set_state(Notify.content)
    await show(cb, "✏️ Отправьте новое сообщение:", akb.sn_cancel())


@router.callback_query(Notify.confirm, F.data.regexp(r"^sn:cancel:\d{1,18}$"))
async def cancel(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer("Отменено")
    await _drop(svc, parse_int(cb.data.split(":")[2]))
    await state.clear()
    await show(cb, "❌ Рассылка отменена.", akb.admin_menu())


@router.callback_query(Notify.confirm, F.data.regexp(r"^sn:send:\d{1,18}$"))
async def send(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    rid = parse_int(cb.data.split(":")[2])
    if rid != (await state.get_data()).get("rid"):
        return
    await state.clear()
    try:
        total = await svc.result_notify.start(rid, cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.admin_menu())
        return
    await show(cb, f"📤 Рассылка #{rid} запущена. Получателей: <b>{total}</b>.\nПо завершении пришлю отчёт.", akb.admin_menu())


@router.callback_query(F.data.regexp(r"^sn:retry:\d{1,18}$"))
async def retry(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    rid = parse_int(cb.data.split(":")[2])
    try:
        total = await svc.result_notify.retry_failed(rid, cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.admin_menu())
        return
    await show(cb, f"🔄 Повтор запущен. Получателей: <b>{total}</b>.", akb.admin_menu())
