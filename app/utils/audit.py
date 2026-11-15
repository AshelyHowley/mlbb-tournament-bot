"""Журналы для админ-панели: история регистраций и действия."""
from __future__ import annotations

from typing import Optional


async def log_registration(
    conn,
    user_id: int,
    action: str,
    team_name: Optional[str] = None,
    details: Optional[str] = None,
    *,
    team_id: Optional[int] = None,
    member_role: Optional[str] = None,
    roles: Optional[str] = None,
    stars: Optional[int] = None,
    position: Optional[str] = None,
) -> None:
    """Запись в историю регистраций со снимком участника: @username и имя берутся из users
    на момент события, чтобы история не менялась, если человек потом сменит ник."""
    u = await conn.fetchrow("SELECT username, full_name FROM users WHERE telegram_id = $1", user_id)
    await conn.execute(
        "INSERT INTO registration_log (user_id, action, team_name, details, username, full_name, team_id, "
        "member_role, roles, stars, position, tournament_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, (SELECT id FROM tournaments WHERE status = 'active'))",
        user_id, action, team_name, details,
        u["username"] if u else None, u["full_name"] if u else None,
        team_id, member_role, roles, stars, position,
    )


async def log_action(conn, user_id: int | None, username: str | None, action: str, details: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO action_log (user_id, username, action, details) VALUES ($1, $2, $3, $4)",
        user_id, username, action, details,
    )
