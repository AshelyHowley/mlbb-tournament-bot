"""Admin panel → «Проверка участников»: channel/chat subscription and @username audits of everyone
registered in the active tournament, with an option to message the offenders through the bot."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.context import Services
from app.handlers.common import IsStaff, show
from app.keyboards import admin as akb
from app.keyboards import common as ckb
from app.services.participant_check import AuditReport
from app.utils.text import esc

log = logging.getLogger(__name__)
router = Router(name="admin_checks")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

MAX_LISTED = 40  # keeps the report well under Telegram's 4096-character limit
MENU_TEXT = (
    "🔎 <b>Проверка участников</b>\n\n"
    "Проверяются все игроки активного турнира (составы команд и соло).\n\n"
    "📢 <b>Канал и чат</b> — состоит ли каждый в канале и чате турнира.\n"
    "🔤 <b>Username</b> — у кого нет @username; таким участникам бот сразу напишет, что его нужно создать."
)


def _listing(rep: AuditReport, fmt) -> list[str]:
    lines = [f"{i}. {fmt(o)}" for i, o in enumerate(rep.offenders[:MAX_LISTED], 1)]
    if len(rep.offenders) > MAX_LISTED:
        lines.append(f"… и ещё {len(rep.offenders) - MAX_LISTED}")
    return lines


def _sub_flags(o) -> str:
    missing = [name for ok, name in ((o.channel_ok, "канал"), (o.chat_ok, "чат")) if not ok]
    return f"{esc(o.p.label)} — нет: {', '.join(missing)}"


def sub_report(rep: AuditReport) -> str:
    lines = ["📢 <b>Проверка подписки на канал и чат</b>\n", f"Проверено участников: <b>{rep.total}</b>"]
    if not rep.offenders and not rep.errors:
        lines.append("\n✅ Все участники выполняют условия.")
    if rep.offenders:
        lines.append(f"❌ Не выполняют условия: <b>{len(rep.offenders)}</b>\n")
        lines += _listing(rep, _sub_flags)
    if rep.errors:
        lines.append(f"\n⚠️ Не удалось проверить (ошибка Telegram): {len(rep.errors)}")
    if rep.notified or rep.not_delivered:
        lines.append(f"\n📨 Напоминание отправлено: {rep.notified}")
        if rep.not_delivered:
            lines.append(f"🚫 Не доставлено (бот заблокирован): {rep.not_delivered}")
    return "\n".join(lines)


def uname_report(rep: AuditReport) -> str:
    lines = ["🔤 <b>Проверка username</b>\n", f"Проверено участников: <b>{rep.total}</b>"]
    if not rep.offenders:
        lines.append("\n✅ У всех участников есть username.")
        return "\n".join(lines)
    lines.append(f"❌ Без username: <b>{len(rep.offenders)}</b>\n")
    lines += _listing(rep, lambda o: esc(o.p.label))
    lines.append(f"\n📨 Сообщение отправлено: {rep.notified}")
    if rep.not_delivered:
        lines.append(f"🚫 Не доставлено (бот заблокирован): {rep.not_delivered}")
    return "\n".join(lines)


@router.callback_query(F.data == "chk:menu")
async def checks_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    await show(cb, MENU_TEXT, akb.checks_menu())


@router.callback_query(F.data.in_({"chk:sub", "chk:subnotify"}))
async def check_subscriptions(cb: CallbackQuery, svc: Services) -> None:
    notify = cb.data == "chk:subnotify"
    await cb.answer("⏳ Проверяю участников…")
    await show(cb, "⏳ Проверяю участников, это может занять немного времени…")
    markup = ckb.subscribe_links(svc.cfg) if notify else None
    rep = await svc.check.subscriptions(notify=notify, markup=markup)
    await show(cb, sub_report(rep), akb.checks_sub_result(bool(rep.offenders) and not notify))


@router.callback_query(F.data == "chk:uname")
async def check_usernames(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer("⏳ Проверяю участников…")
    await show(cb, "⏳ Проверяю участников, это может занять немного времени…")
    rep = await svc.check.usernames(notify=True)
    await show(cb, uname_report(rep), akb.checks_back())
