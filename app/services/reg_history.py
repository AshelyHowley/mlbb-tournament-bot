"""«📋 История регистраций»: карточки для Telegram и полная выгрузка в CSV (открывается в Excel)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.utils.text import esc

PAGE_SIZE = 8

ACTIONS = {
    "solo": "👤 Соло-регистрация",
    "create_team": "🛡 Создал команду",
    "join_team": "➕ Вступил в команду",
    "captain": "👑 Стал капитаном",
    "captain_transfer": "🔁 Получил капитанство",
    "leave": "🚪 Вышел из команды",
    "removed": "❌ Удалён из команды",
    "cancel_team": "🗑 Отменил регистрацию команды",
    "unregister_team": "🗑 Снял команду с регистрации",
    "unregister_solo": "🗑 Снялся с соло-регистрации",
    "admin_delete_team": "⛔ Команда снята админом",
    "admin_delete_solo": "⛔ Соло снят админом",
}
MEMBER_ROLES = {"captain": "👑 капитан", "member": "🎮 игрок", "registrar": "📝 регистратор"}

# COALESCE: у старых записей (до миграции 022) снимка нет - берём текущие данные пользователя
_SELECT = (
    "SELECT l.id, l.user_id, l.action, l.team_name, l.details, l.created_at, l.member_role, l.roles, "
    "       l.stars, l.position, l.team_id, "
    "       COALESCE(l.username, u.username) AS username, COALESCE(l.full_name, u.full_name) AS full_name "
    "  FROM registration_log l LEFT JOIN users u ON u.telegram_id = l.user_id "
)


def _local(created_at: str, tz: str) -> datetime:
    dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz))


def _roles(r) -> Optional[str]:
    roles = r["roles"]
    if not roles and r["action"] == "solo" and r["details"]:
        roles = r["details"]  # старый формат: роли лежали в details
    return ", ".join(x.strip() for x in roles.split(",")) if roles else None


def _member_role(r) -> Optional[str]:
    role = r["member_role"]
    if not role and r["details"] and r["details"].startswith("role="):
        role = r["details"][5:]  # старый формат create_team: 'role=captain'
    return role


def _details(r) -> Optional[str]:
    d = r["details"]
    if not d or (r["action"] == "solo" and not r["roles"]) or d.startswith("role="):
        return None
    return d


async def page(conn, page_no: int) -> tuple[int, int, int, list]:
    total = await conn.fetchval("SELECT count(*) FROM registration_log")
    last = max((total - 1) // PAGE_SIZE, 0)
    page_no = min(max(page_no, 0), last)
    rows = await conn.fetch(_SELECT + " ORDER BY l.id DESC LIMIT $1 OFFSET $2", PAGE_SIZE, page_no * PAGE_SIZE)
    return total, page_no, last, rows


def card(r, tz: str) -> str:
    when = _local(r["created_at"], tz).strftime("%d.%m.%Y %H:%M")
    lines = [f"<b>{when}</b> · {ACTIONS.get(r['action'], esc(r['action']))}"]
    who = [esc(r["full_name"]) if r["full_name"] else "без имени"]
    if r["username"]:
        who.append(f"@{esc(r['username'])}")
    who.append(f"ID <code>{r['user_id']}</code>")
    lines.append(" · ".join(who))
    info = []
    if r["team_name"]:
        info.append(f"🛡 «{esc(r['team_name'])}»")
    role = _member_role(r)
    if role:
        info.append(MEMBER_ROLES.get(role, esc(role)))
    roles = _roles(r)
    if roles:
        info.append(f"🎯 {esc(roles)}")
    if r["stars"] is not None:
        info.append(f"⭐ {r['stars']}")
    if r["position"]:
        info.append(f"💼 {esc(r['position'])}")
    if info:
        lines.append(" · ".join(info))
    extra = _details(r)
    if extra:
        lines.append(f"<i>{esc(extra)}</i>")
    return "\n".join(lines)


def render_page(rows, total: int, page_no: int, last: int, tz: str) -> str:
    if not rows:
        return "📋 <b>История регистраций</b>\n\nПока пусто."
    head = f"📋 <b>История регистраций</b>\nВсего записей: <b>{total}</b> · стр. {page_no + 1}/{last + 1}"
    return head + "\n\n" + "\n\n".join(card(r, tz) for r in rows)


CSV_HEADER = ["Дата и время", "Событие", "Telegram ID", "Username", "Имя", "Команда", "Роль в команде",
              "Роли (соло)", "Звёзды", "Должность", "Подробности"]


async def export_csv(conn, tz: str) -> bytes:
    """Вся история одним файлом. ';' и UTF-8 с BOM - чтобы русский Excel открыл двойным щелчком."""
    rows = await conn.fetch(_SELECT + " ORDER BY l.id")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_HEADER)
    for r in rows:
        role = _member_role(r)
        w.writerow([
            _local(r["created_at"], tz).strftime("%d.%m.%Y %H:%M:%S"),
            ACTIONS.get(r["action"], r["action"]).split(" ", 1)[-1],
            r["user_id"],
            f"@{r['username']}" if r["username"] else "",
            r["full_name"] or "",
            r["team_name"] or "",
            MEMBER_ROLES.get(role, role or "").split(" ", 1)[-1] if role else "",
            _roles(r) or "",
            "" if r["stars"] is None else r["stars"],
            r["position"] or "",
            _details(r) or "",
        ])
    return buf.getvalue().encode("utf-8-sig")
