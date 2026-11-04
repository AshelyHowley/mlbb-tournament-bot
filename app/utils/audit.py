"""Simple audit logging for admin history."""
from __future__ import annotations


async def log_registration(conn, user_id: int, action: str, team_name: str | None = None, details: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO registration_log (user_id, action, team_name, details) VALUES ($1, $2, $3, $4)",
        user_id, action, team_name, details,
    )


async def log_action(conn, user_id: int | None, username: str | None, action: str, details: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO action_log (user_id, username, action, details) VALUES ($1, $2, $3, $4)",
        user_id, username, action, details,
    )
