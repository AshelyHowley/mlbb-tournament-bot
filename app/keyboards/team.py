from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from app.keyboards.common import kb
from app.utils.text import person_plain

_MENU = B(text="◀️ В меню", callback_data="menu")


def after_create() -> InlineKeyboardMarkup:
    """Captain just created / accepted a team."""
    return kb([
        [B(text="👥 Моя команда", callback_data="team")],
        [B(text="🔗 Пригласить игрока", callback_data="team:inv")],
        [_MENU],
    ])


def after_create_pending() -> InlineKeyboardMarkup:
    """Team created, captain still to be assigned."""
    return kb([
        [B(text="👥 Моя команда", callback_data="team")],
        [B(text="🔗 Ссылка капитана", callback_data="team:cap")],
        [_MENU],
    ])


def my_team_only() -> InlineKeyboardMarkup:
    return kb([[B(text="👥 Моя команда", callback_data="team")], [_MENU]])


def invitation(token: str) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Вступить в команду", callback_data=f"join:ok:{token}")],
        [B(text="❌ Отмена", callback_data="menu")],
    ])


def captain_invitation(token: str) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Стать капитаном", callback_data=f"cap:ok:{token}")],
        [B(text="❌ Отказаться", callback_data="menu")],
    ])


def team_card(role: str, *, pending: bool, is_creator: bool, can_transfer: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if role == "captain":
        rows.append([B(text="🔗 Пригласить игрока", callback_data="team:inv")])
        rows.append([B(text="🔄 Управление составом", callback_data="team:mng")])
        if can_transfer:
            rows.append([B(text="👑 Передать капитанство", callback_data="team:tr")])
    else:
        if pending and is_creator:  # the registrar's tools exist only until a captain is assigned
            rows.append([B(text="🔗 Ссылка капитана", callback_data="team:cap")])
            rows.append([B(text="🔄 Обновить ссылку капитана", callback_data="team:capreset")])
            rows.append([B(text="🗑 Отменить регистрацию", callback_data="team:cancel")])
        elif role == "member":
            rows.append([B(text="🚪 Покинуть команду", callback_data="team:leave")])
    rows.append([_MENU])
    return kb(rows)


def captain_link_screen() -> InlineKeyboardMarkup:
    return kb([
        [B(text="🔄 Обновить ссылку капитана", callback_data="team:capreset")],
        [B(text="⬅️ Назад", callback_data="team")],
    ])


def back_to_team() -> InlineKeyboardMarkup:
    return kb([[B(text="⬅️ Назад", callback_data="team")]])


def manage_list(members: Sequence) -> InlineKeyboardMarkup:
    rows = [
        [B(text=person_plain(m.full_name, m.username, m.user_id)[:50], callback_data=f"team:m:{m.user_id}")]
        for m in members if not m.is_captain
    ]
    return kb(rows + [[B(text="⬅️ Назад", callback_data="team")]])


def transfer_list(members: Sequence) -> InlineKeyboardMarkup:
    rows = [
        [B(text=person_plain(m.full_name, m.username, m.user_id)[:50], callback_data=f"team:trc:{m.user_id}")]
        for m in members if not m.is_captain
    ]
    return kb(rows + [[B(text="⬅️ Назад", callback_data="team")]])


def member_card(uid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="🔄 Заменить", callback_data=f"team:rp:{uid}")],
        [B(text="🗑 Удалить из команды", callback_data=f"team:rm:{uid}")],
        [B(text="⬅️ Назад", callback_data="team:mng")],
    ])


def confirm(ok_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    return kb([[B(text="✅ Подтвердить", callback_data=ok_cb)], [B(text="❌ Отмена", callback_data=cancel_cb)]])
