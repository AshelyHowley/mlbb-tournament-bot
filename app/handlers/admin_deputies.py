"""Deputy management. MAIN ADMIN only, end to end - a deputy may never invite/approve/reject/remove
another deputy, escalate their own rights, or change who the main admin is."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.context import Services
from app.handlers.common import IsAdmin, show
from app.keyboards import admin as akb
from app.services.errors import ServiceError
from app.utils.text import esc, parse_int, person

router = Router(name="admin_deputies")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

DEPUTY_TITLE = "🛡 <b>Заместители</b>"


@router.callback_query(F.data == "dep:menu")
async def menu(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    pending = await svc.deputies.pending()
    active = await svc.deputies.list_active()
    await show(cb, DEPUTY_TITLE, akb.deputy_menu(len(pending), len(active)))


@router.callback_query(F.data == "dep:list")
async def list_active(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    rows = await svc.deputies.list_active()
    text = DEPUTY_TITLE + f"\n\nАктивных: <b>{len(rows)}/2</b>" + ("" if rows else "\n\nПока никого нет.")
    await show(cb, text, akb.deputy_list(rows))


@router.callback_query(F.data.regexp(r"^dep:card:\d{1,18}$"))
async def card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = parse_int(cb.data.split(":")[2])
    rows = await svc.deputies.list_active()
    row = next((r for r in rows if r["user_id"] == uid), None)
    if not row:
        await show(cb, "Заместитель не найден (возможно, уже снят).", akb.deputy_list(rows))
        return
    await show(
        cb, f"🛡 <b>Заместитель</b>\n\n{person(row['full_name'], row['username'], row['user_id'])}\n"
        f"Назначен: {row['approved_at'].strftime('%d.%m.%Y %H:%M')}", akb.deputy_card(uid),
    )


@router.callback_query(F.data.regexp(r"^dep:revoke:\d{1,18}$"))
async def revoke(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = parse_int(cb.data.split(":")[2])
    try:
        await svc.deputies.revoke(cb.from_user.id, uid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.deputy_menu(0, 0))
        return
    await show(cb, "✅ Полномочия заместителя сняты.", akb.deputy_menu(0, 0))
    try:
        await cb.bot.send_message(uid, "ℹ️ Главный администратор снял с вас полномочия заместителя турнира.")
    except Exception:
        pass


@router.callback_query(F.data == "dep:invite")
async def invite(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        token = await svc.deputies.create_invite(cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.deputy_menu(0, 0))
        return
    me = await cb.bot.me()
    link = f"https://t.me/{me.username}?start=deputy_{token}"
    await show(
        cb,
        f"🛡 <b>Приглашение заместителя</b>\n\nОтправьте эту ссылку будущему заместителю:\n\n<code>{esc(link)}</code>\n\n"
        "После того как он примет приглашение, здесь появится заявка на подтверждение.",
        akb.to_admin(),
    )


@router.callback_query(F.data == "dep:pending")
async def pending(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    rows = await svc.deputies.pending()
    text = DEPUTY_TITLE + "\n\n📋 Заявки на рассмотрении" + ("" if rows else "\n\nПока нет заявок.")
    await show(cb, text, akb.deputy_pending_list(rows))


@router.callback_query(F.data.regexp(r"^dep:review:\d{1,18}$"))
async def review(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    invite_id = parse_int(cb.data.split(":")[2])
    rows = await svc.deputies.pending()
    row = next((r for r in rows if r["id"] == invite_id), None)
    if not row:
        await show(cb, "Заявка не найдена (возможно, уже рассмотрена).", akb.deputy_menu(0, 0))
        return
    await show(
        cb, f"🛡 <b>Новый заместитель</b>\n\n👤 Имя: {person(row['full_name'], row['username'], row['candidate_id'])}\n"
        f"🆔 Telegram ID: {row['candidate_id']}", akb.deputy_review(invite_id),
    )


@router.callback_query(F.data.regexp(r"^dep:approve:\d{1,18}$"))
async def approve(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    invite_id = parse_int(cb.data.split(":")[2])
    try:
        inv = await svc.deputies.approve(cb.from_user.id, invite_id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.deputy_menu(0, 0))
        return
    await show(cb, "✅ Заместитель подтверждён.", akb.deputy_menu(0, 0))
    try:
        await cb.bot.send_message(inv["candidate_id"], "✅ Вы назначены заместителем главного администратора турнира.")
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^dep:reject:\d{1,18}$"))
async def reject(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    invite_id = parse_int(cb.data.split(":")[2])
    rows = await svc.deputies.pending()
    row = next((r for r in rows if r["id"] == invite_id), None)
    try:
        await svc.deputies.reject(cb.from_user.id, invite_id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.deputy_menu(0, 0))
        return
    await show(cb, "❌ Заявка отклонена.", akb.deputy_menu(0, 0))
    if row:
        try:
            await cb.bot.send_message(row["candidate_id"], "❌ Ваша заявка на роль заместителя отклонена.")
        except Exception:
            pass
