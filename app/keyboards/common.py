from __future__ import annotations

from typing import Optional, Sequence

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from app.config import Config
from app.services.registration import ROLES, MyRegistration


def kb(rows: Sequence[Sequence[B]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def main_menu(cfg: Config, reg: Optional[MyRegistration], is_admin: bool) -> InlineKeyboardMarkup:
    rows = []
    if reg is None or not reg.plays:  # a registrar-only user does not play yet and may still register / join
        rows.append([B(text="🏆 Регистрация", callback_data="reg:start")])
    if reg is not None:
        rows.append([B(text="👤 Моя регистрация", callback_data="me")])
        if reg.has_team:
            rows.append([B(text="👥 Моя команда", callback_data="team")])
    rows.append([B(text="🏆 Турнирная сетка", callback_data="bracket")])
    rows.append([B(text="🏅 Зал славы", callback_data="hof")])
    rows.append([B(text="📢 Канал", url=cfg.channel_url), B(text="💬 Чат", url=cfg.chat_url)])
    if is_admin:
        rows.append([B(text="🛠 Админ-панель", callback_data="adm")])
    return kb(rows)

def back_to_menu() -> InlineKeyboardMarkup:
    return kb([[B(text="◀️ В меню", callback_data="menu")]])


def reg_type() -> InlineKeyboardMarkup:
    return kb([
        [B(text="👥 Зарегистрировать команду", callback_data="reg:team")],
        [B(text="👤 Соло-регистрация", callback_data="reg:solo")],
        [B(text="◀️ В меню", callback_data="menu")],
    ])


def nav(back: bool = True) -> InlineKeyboardMarkup:
    row = []
    if back:
        row.append(B(text="◀️ Назад", callback_data="reg:back"))
    row.append(B(text="❌ Отмена", callback_data="reg:cancel"))
    return kb([row])


def team_role() -> InlineKeyboardMarkup:
    return kb([
        [B(text="👑 Я капитан", callback_data="reg:trole:captain")],
        [B(text="🎮 Я участник, капитан другой", callback_data="reg:trole:member")],
        [B(text="📝 Я только регистрирую команду", callback_data="reg:trole:registrar")],
        [B(text="❌ Отмена", callback_data="reg:cancel")],
    ])


def confirm_reg() -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Подтвердить", callback_data="reg:confirm")],
        [B(text="◀️ Назад", callback_data="reg:back"), B(text="❌ Отмена", callback_data="reg:cancel")],
    ])


def roles() -> InlineKeyboardMarkup:
    rows = [[B(text=r, callback_data=f"reg:role:{r}") for r in ROLES[:3]], [B(text=r, callback_data=f"reg:role:{r}") for r in ROLES[3:]]]
    rows.append([B(text="❌ Отмена", callback_data="reg:cancel")])
    return kb(rows)


def subscribe(cfg: Config, retry_cb: str = "sub:check", cancel_cb: str = "reg:cancel") -> InlineKeyboardMarkup:
    return kb([
        [B(text="📢 Подписаться на канал", url=cfg.channel_url)],
        [B(text="💬 Вступить в чат", url=cfg.chat_url)],
        [B(text="🔄 Проверить подписку", callback_data=retry_cb)],
        [B(text="❌ Отмена", callback_data=cancel_cb)],
    ])


def my_reg_menu(can_unregister: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if can_unregister:
        rows.append([B(text="❌ Сняться с регистрации", callback_data="me:unregister")])
    rows.append([B(text="◀️ В меню", callback_data="menu")])
    return kb(rows)


def roles_multi(selected: list[str] | None = None) -> InlineKeyboardMarkup:
    selected = selected or []
    rows = []
    for r in ROLES:
        mark = "✅ " if r in selected else ""
        rows.append([B(text=f"{mark}{r}", callback_data=f"reg:role:{r}")])
    rows.append([B(text="🌟 Все роли", callback_data="reg:role:ALL")])
    rows.append([B(text="✅ Готово", callback_data="reg:role:done")])
    rows.append([B(text="◀️ Назад", callback_data="reg:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
