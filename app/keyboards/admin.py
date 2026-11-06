from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from app.keyboards.common import kb
from app.services.broadcast import AUDIENCES
from app.utils.text import person_plain


def admin_menu(is_main_admin: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [B(text="👥 Список команд", callback_data="adm:t:0"), B(text="👤 Соло-игроки", callback_data="adm:s:0")],
        [B(text="🏆 Турнирная сетка", callback_data="adm:bracket")],
        [B(text="📢 Рассылка", callback_data="adm:bc"), B(text="🔐 Регистрация", callback_data="adm:reg")],
        [B(text="📋 История регистраций", callback_data="log:reg"), B(text="📜 Логи действий", callback_data="log:act")],
        [B(text="📚 История турниров", callback_data="hist:0"), B(text="🏁 Завершить турнир", callback_data="tf:start")],
    ]
    if is_main_admin:
        rows.append([B(text="🛡 Заместители", callback_data="dep:menu")])
    rows.append([B(text="◀️ В меню", callback_data="menu")])
    return kb(rows)


def to_admin() -> InlineKeyboardMarkup:
    return kb([[B(text="◀️ Админ-панель", callback_data="adm")]])


def pager(prefix: str, page: int, last: int) -> list[B]:
    row = []
    if page > 0:
        row.append(B(text="⬅️", callback_data=f"{prefix}:{page - 1}"))
    row.append(B(text=f"{page + 1}/{last + 1}", callback_data="noop"))
    if page < last:
        row.append(B(text="➡️", callback_data=f"{prefix}:{page + 1}"))
    return row


def teams_list(rows: Sequence, page: int, last: int) -> InlineKeyboardMarkup:
    items = [[B(text=r["name"][:40], callback_data=f"adm:tc:{r['id']}")] for r in rows]
    return kb(items + [
        [B(text="🎲 Собрать команды из соло", callback_data="rnd:solo")],
        pager("adm:t", page, last),
        [B(text="◀️ Админ-панель", callback_data="adm")],
    ])


def team_card(team_id: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="❌ Снять с регистрации", callback_data=f"adm:td:{team_id}")],
        [B(text="◀️ К списку", callback_data="adm:t:0")],
    ])


def confirm_delete(ok_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    return kb([
        [B(text="⚠️ Подтвердить снятие", callback_data=ok_cb)],
        [B(text="❌ Отмена", callback_data=cancel_cb)],
    ])


def solo_list(rows: Sequence, page: int, last: int) -> InlineKeyboardMarkup:
    items = [[B(text=f"{person_plain(r['full_name'], r['username'], r['user_id'])[:40]} — {r['role']}", callback_data=f"adm:sc:{r['user_id']}")] for r in rows]
    return kb(items + [pager("adm:s", page, last), [B(text="◀️ Админ-панель", callback_data="adm")]])


def solo_card(uid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="❌ Снять с регистрации", callback_data=f"adm:sd:{uid}")],
        [B(text="◀️ К списку", callback_data="adm:s:0")],
    ])


def registration(open_: bool) -> InlineKeyboardMarkup:
    if open_:
        btn = B(text="🔒 Закрыть регистрацию", callback_data="adm:reg:set:0")
    else:
        btn = B(text="🔓 Открыть регистрацию", callback_data="adm:reg:set:1")
    return kb([[btn], [B(text="◀️ Админ-панель", callback_data="adm")]])


# ---- matches ----
def matches_menu() -> InlineKeyboardMarkup:
    return kb([
        [B(text="➕ Создать матч", callback_data="mt:new")],
        [B(text="⏳ Ожидающие матчи / победитель", callback_data="mt:l:scheduled:0")],
        [B(text="✅ Завершённые", callback_data="mt:l:finished:0")],
        [B(text="◀️ Админ-панель", callback_data="adm")],
    ])


def pick_team(rows: Sequence, prefix: str, page: int, last: int, exclude: int = 0) -> InlineKeyboardMarkup:
    items = [[B(text=r["name"][:40], callback_data=f"{prefix}:pick:{r['id']}")] for r in rows if r["id"] != exclude]
    nav = pager(f"{prefix}:pg", page, last) if last > 0 else []
    return kb(items + ([nav] if nav else []) + [[B(text="❌ Отмена", callback_data="mt:cancel")]])


def pick_stage(stages: Sequence) -> InlineKeyboardMarkup:
    items = [[B(text=s["title"], callback_data=f"mt:stage:{s['code']}")] for s in stages]
    return kb(items + [[B(text="❌ Отмена", callback_data="mt:cancel")]])


def confirm_match() -> InlineKeyboardMarkup:
    return kb([[B(text="✅ Создать матч", callback_data="mt:ok")], [B(text="❌ Отмена", callback_data="mt:cancel")]])


def match_list(rows: Sequence, status: str, page: int, last: int) -> InlineKeyboardMarkup:
    items = [[B(text=f"#{r['id']} {r['team_a'][:16]} vs {r['team_b'][:16]} ({r['stage_title']})", callback_data=f"mt:c:{r['id']}")] for r in rows]
    return kb(items + [pager(f"mt:l:{status}", page, last), [B(text="◀️ Матчи", callback_data="adm:m")]])


def series_card(m) -> InlineKeyboardMarkup:
    rows = []
    if m["status"] == "scheduled":
        rows.append([B(text="🎮 Внести результат", callback_data=f"mt:gnew:{m['id']}")])
    rows.append([B(text="✏️ Исправить результат", callback_data=f"mt:gedit:{m['id']}")])
    if m["status"] == "finished":
        rows.append([B(text="📨 Уведомления", callback_data=f"sn:pick:{m['id']}")])
    rows.append([B(text="🗑 Удалить матч", callback_data=f"mt:d:{m['id']}")])
    rows.append([B(text="◀️ Матчи", callback_data="adm:m")])
    return kb(rows)


def game_winner_pick(m) -> InlineKeyboardMarkup:
    return kb([
        [B(text=f"🏆 {m['team_a'][:28]}", callback_data=f"mt:gw:{m['id']}:{m['team_a_id']}")],
        [B(text=f"🏆 {m['team_b'][:28]}", callback_data=f"mt:gw:{m['id']}:{m['team_b_id']}")],
        [B(text="❌ Отмена", callback_data=f"mt:c:{m['id']}")],
    ])


def confirm_game(mid: int, tid: int, game_no: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Подтвердить", callback_data=f"mt:gok:{mid}:{tid}:{game_no}")],
        [B(text="❌ Отмена", callback_data=f"mt:c:{mid}")],
    ])


def games_edit_list(mid: int, games: Sequence) -> InlineKeyboardMarkup:
    rows = [[B(text=f"Катка {g['game_number']}: 🏆 {g['winner_name'][:24]}", callback_data=f"mt:gec:{g['id']}")] for g in games]
    return kb(rows + [[B(text="⬅️ Назад", callback_data=f"mt:c:{mid}")]])


def game_edit_pick(g, m) -> InlineKeyboardMarkup:
    return kb([
        [B(text=f"🏆 {m['team_a'][:28]}", callback_data=f"mt:gew:{g['id']}:{m['team_a_id']}")],
        [B(text=f"🏆 {m['team_b'][:28]}", callback_data=f"mt:gew:{g['id']}:{m['team_b_id']}")],
        [B(text="❌ Отмена", callback_data=f"mt:c:{m['id']}")],
    ])


def confirm_game_edit(game_id: int, tid: int, mid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Подтвердить", callback_data=f"mt:gewok:{game_id}:{tid}")],
        [B(text="❌ Отмена", callback_data=f"mt:c:{mid}")],
    ])


# ---- broadcast ----
def bc_audience() -> InlineKeyboardMarkup:
    rows = [[B(text=title, callback_data=f"bc:aud:{key}")] for key, title in AUDIENCES.items()]
    rows.append([B(text="📤 Сетка всем", callback_data="qb:bracket:all"), B(text="📤 Сетка капитанам", callback_data="qb:bracket:captains")])
    rows.append([B(text="◀️ Админ-панель", callback_data="adm")])
    return kb(rows)


def bc_cancel() -> InlineKeyboardMarkup:
    return kb([[B(text="❌ Отмена", callback_data="bc:abort")]])


def bc_confirm(bid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="📤 Отправить", callback_data=f"bc:send:{bid}")],
        [B(text="✏️ Изменить", callback_data=f"bc:edit:{bid}"), B(text="❌ Отмена", callback_data=f"bc:cancel:{bid}")],
    ])


# ---- series/final result notifications ----
def sn_audience_series(mid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="🏆 Победителям", callback_data=f"sn:aud:series_winners:{mid}")],
        [B(text="❌ Проигравшим", callback_data=f"sn:aud:series_losers:{mid}")],
        [B(text="⬅️ Назад", callback_data=f"mt:c:{mid}")],
    ])


def sn_audience_final(tournament_id: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="👑 1 месту", callback_data=f"sn:aud:final_first:{tournament_id}")],
        [B(text="🥈 2 месту", callback_data=f"sn:aud:final_second:{tournament_id}")],
        [B(text="🥉 3 месту", callback_data=f"sn:aud:final_third:{tournament_id}")],
        [B(text="🎮 Остальным участникам", callback_data=f"sn:aud:final_others:{tournament_id}")],
        [B(text="👥 Всем участникам", callback_data=f"sn:aud:final_all:{tournament_id}")],
        [B(text="◀️ Админ-панель", callback_data="adm")],
    ])


def sn_cancel() -> InlineKeyboardMarkup:
    return kb([[B(text="❌ Отмена", callback_data="sn:abort")]])


def sn_confirm(rid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Отправить", callback_data=f"sn:send:{rid}")],
        [B(text="✏️ Изменить", callback_data=f"sn:edit:{rid}"), B(text="❌ Отмена", callback_data=f"sn:cancel:{rid}")],
    ])


# ---- deputies ----
def deputy_menu(pending_count: int, active_count: int) -> InlineKeyboardMarkup:
    rows = [[B(text=f"👥 Активные заместители ({active_count}/2)", callback_data="dep:list")]]
    if pending_count:
        rows.append([B(text=f"📋 Заявки на рассмотрении ({pending_count})", callback_data="dep:pending")])
    if active_count < 2:
        rows.append([B(text="➕ Пригласить заместителя", callback_data="dep:invite")])
    rows.append([B(text="◀️ Админ-панель", callback_data="adm")])
    return kb(rows)


def deputy_list(rows: Sequence) -> InlineKeyboardMarkup:
    items = [[B(text=f"🛡 {person_plain(r['full_name'], r['username'], r['user_id'])[:36]}", callback_data=f"dep:card:{r['user_id']}")] for r in rows]
    return kb(items + [[B(text="◀️ Назад", callback_data="dep:menu")]])


def deputy_card(uid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="🗑 Снять полномочия", callback_data=f"dep:revoke:{uid}")],
        [B(text="◀️ Назад", callback_data="dep:list")],
    ])


def deputy_pending_list(rows: Sequence) -> InlineKeyboardMarkup:
    items = [[B(text=f"🛡 {person_plain(r['full_name'], r['username'], r['candidate_id'])[:36]}", callback_data=f"dep:review:{r['id']}")] for r in rows]
    return kb(items + [[B(text="◀️ Назад", callback_data="dep:menu")]])


def deputy_review(invite_id: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Подтвердить", callback_data=f"dep:approve:{invite_id}")],
        [B(text="❌ Отклонить", callback_data=f"dep:reject:{invite_id}")],
        [B(text="◀️ Назад", callback_data="dep:pending")],
    ])


def deputy_invitation(token: str) -> InlineKeyboardMarkup:
    return kb([
        [B(text="✅ Принять приглашение", callback_data=f"dep:acc:{token}")],
        [B(text="❌ Отказаться", callback_data="menu")],
    ])


# ---- tournament finish / history ----
def finish_confirm(ready: bool) -> InlineKeyboardMarkup:
    if ready:
        return kb([[B(text="⚠️ Продолжить", callback_data="tf:go")], [B(text="❌ Отмена", callback_data="adm")]])
    return kb([[B(text="⚠️ Завершить досрочно", callback_data="tf:early")], [B(text="❌ Отмена", callback_data="adm")]])


def finish_final() -> InlineKeyboardMarkup:
    return kb([[B(text="🏁 ЗАВЕРШИТЬ ТУРНИР", callback_data="tf:ok")], [B(text="❌ Отмена", callback_data="adm")]])


def finish_done() -> InlineKeyboardMarkup:
    return kb([[B(text="📚 Открыть историю", callback_data="hist:0")], [B(text="🏠 Главное меню", callback_data="menu")]])


def history_list(rows: Sequence, page: int, last: int) -> InlineKeyboardMarkup:
    items = [[B(text=f"🏆 {(r['completed_at'][:16].replace('T',' ') if isinstance(r['completed_at'], str) else r['completed_at'].strftime('%d.%m.%Y %H:%M')) if r['completed_at'] else '—'}", callback_data=f"hist:c:{r['id']}")] for r in rows]
    return kb(items + [pager("hist", page, last), [B(text="◀️ Админ-панель", callback_data="adm")]])


def history_card(tid: int) -> InlineKeyboardMarkup:
    return kb([
        [B(text="🏆 Турнирная сетка", callback_data=f"hist:b:{tid}")],
        [B(text="👥 Список команд", callback_data=f"hist:t:{tid}"), B(text="👤 Соло-участники", callback_data=f"hist:s:{tid}")],
        [B(text="🎮 Матчи и результаты", callback_data=f"hist:m:{tid}")],
        [B(text="📊 Статистика", callback_data=f"hist:stat:{tid}")],
        [B(text="⬅️ К истории", callback_data="hist:0")],
    ])


def history_back(tid: int) -> InlineKeyboardMarkup:
    return kb([[B(text="⬅️ Назад", callback_data=f"hist:c:{tid}")]])


def history_teams_list(rows: Sequence, tid: int) -> InlineKeyboardMarkup:
    items = [[B(text=r["name"][:40], callback_data=f"hist:tc:{r['id']}:{tid}")] for r in rows]
    return kb(items + [[B(text="⬅️ Назад", callback_data=f"hist:c:{tid}")]])


def history_matches_list(rows: Sequence, tid: int) -> InlineKeyboardMarkup:
    items = [[B(text=f"{r['stage_title']}: {r['team_a'][:16]} vs {r['team_b'][:16]}", callback_data=f"hist:mc:{r['id']}:{tid}")] for r in rows]
    return kb(items + [[B(text="⬅️ Назад", callback_data=f"hist:c:{tid}")]])
