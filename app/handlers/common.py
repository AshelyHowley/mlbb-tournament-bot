from __future__ import annotations

import logging
from typing import Optional, Union

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from app.bot.context import Services
from app.keyboards.common import main_menu
from app.services.registration import MyRegistration, TgUser
from app.services.team import MAX_TEAM_SIZE, TeamView
from app.utils.text import esc, person

log = logging.getLogger(__name__)


class IsAdmin(BaseFilter):
    """Server-side check for the MAIN ADMIN only (ADMIN_TELEGRAM_ID). Used for the handful of actions
    a deputy may never perform: appointing/removing deputies. Nothing in callback_data grants access."""

    async def __call__(self, event: TelegramObject, svc: Services) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and svc.cfg.is_bot_admin(user.id)


class IsStaff(BaseFilter):
    """Server-side check for MAIN ADMIN or an active DEPUTY - the broad "run the tournament" role.
    A removed deputy fails this on their very next request (re-checked from PostgreSQL every time)."""

    async def __call__(self, event: TelegramObject, svc: Services) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and await svc.roles.is_staff(user.id)


def tg_user(event: Union[Message, CallbackQuery]) -> TgUser:
    u = event.from_user
    return TgUser(id=u.id, username=u.username, full_name=u.full_name or "")


_last_bot_msg: dict[int, int] = {}

async def show(event: Union[Message, CallbackQuery], text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Edit or send message, delete previous to keep chat clean."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        chat_id = event.from_user.id
        if isinstance(msg, Message):
            try:
                await msg.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
            except TelegramAPIError:
                pass
        # Delete previous
        prev = _last_bot_msg.get(chat_id)
        if prev:
            try:
                await event.bot.delete_message(chat_id, prev)
            except Exception:
                pass
        sent = await event.bot.send_message(chat_id, text, reply_markup=markup)
        _last_bot_msg[chat_id] = sent.message_id
    else:
        chat_id = event.chat.id
        prev = _last_bot_msg.get(chat_id)
        if prev:
            try:
                await event.bot.delete_message(chat_id, prev)
            except Exception:
                pass
        sent = await event.answer(text, reply_markup=markup)
        _last_bot_msg[chat_id] = sent.message_id


def format_solo(reg: MyRegistration, user_id: int) -> str:
    stars = getattr(reg, "stars", 0) or 0
    stars_line = f"\n⭐ Звёзды: <b>{stars}</b>" if stars else ""
    return (
        "👤 <b>Ваша регистрация: соло</b>\n\n"
        f"Telegram: {person(reg.full_name, reg.username, user_id)}\nРоль: <b>{esc(reg.role)}</b>{stars_line}\nСтатус: ✅ Зарегистрирован"
    )


_ROLE_LINE = {"captain": "👑 Ваша роль: Капитан", "member": "🎮 Ваша роль: Участник", "registrar": "📝 Ваша роль: Регистратор"}


def format_team(view: TeamView, viewer_id: Optional[int] = None, *, title: Optional[str] = None) -> str:
    """Team card as seen by `viewer_id` (captain / member / registrar)."""
    role = view.role_of(viewer_id) if viewer_id is not None else None
    cap = view.captain
    lines = [title or "👥 <b>Моя команда</b>", "", f"🏆 Команда: <b>{esc(view.name)}</b>"]
    if role:
        lines.append(_ROLE_LINE[role])
    if role != "captain":
        lines.append(f"👑 Капитан: {cap.display if cap else 'ожидается'}")
    if role == "registrar":
        lines.append("(вы не входите в игровой состав)")
    lines.append(f"👥 Состав: {view.size}/{MAX_TEAM_SIZE}")
    if view.members:
        lines.append("")
        lines += [f"{i}. {'👑 ' if m.is_captain else ''}{m.display}" for i, m in enumerate(view.members, 1)]
    if view.captain_pending:
        lines.append("\n⏳ Капитан не назначен. Набор игроков откроется после назначения капитана.")
    elif view.free:
        lines.append(f"\nСвободных мест: {view.free}")
    return "\n".join(lines)

async def menu_view(svc: Services, user_id: int):
    reg = await svc.reg.my_registration(user_id)
    text = "🎮 <b>Турнир Mobile Legends</b>\n\n"
    if reg is None:
        text += "Вы пока не зарегистрированы. Нажмите «Регистрация»."
        if not await svc.reg.is_open():
            text += "\n🔒 Регистрация сейчас закрыта."
    else:
        text += "✅ Вы зарегистрированы."
    return text, main_menu(svc.cfg, reg, await svc.roles.is_staff(user_id))
