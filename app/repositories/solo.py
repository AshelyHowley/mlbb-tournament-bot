from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

_ACTIVE = "(SELECT id FROM tournaments WHERE status = 'active')"


async def insert(conn, user_id: int, username: Optional[str], norm: Optional[str], role: str, stars: int = 0) -> None:
    # Clear any previous solo registration for this user (any tournament)
    await conn.execute("DELETE FROM solo_players WHERE user_id = $1", user_id)
    tid = await conn.fetchval("SELECT id FROM tournaments WHERE status = 'active' LIMIT 1")
    if tid is None:
        await conn.execute("INSERT INTO tournaments (status) VALUES ('active')")
        tid = await conn.fetchval("SELECT id FROM tournaments WHERE status = 'active' LIMIT 1")
    await conn.execute(
        "INSERT INTO solo_players (user_id, username, username_norm, role, tournament_id, stars) VALUES ($1,$2,$3,$4,$5,$6)",
        user_id, username, norm, role, tid, stars,
    )


async def get(conn, user_id: int) -> Optional[dict]:
    """The user's solo registration in the ACTIVE tournament (an archived one no longer counts)."""
    return await conn.fetchrow(
        "SELECT s.user_id, s.username, s.role, s.created_at, COALESCE(s.display_full_name, u.full_name) AS full_name "
        "FROM solo_players s JOIN users u ON u.telegram_id = s.user_id WHERE s.user_id = $1 AND s.tournament_id = " + _ACTIVE,
        user_id,
    )


async def count(conn) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM solo_players WHERE tournament_id = {_ACTIVE}")


async def list_page(conn, limit: int, offset: int) -> list[dict]:
    return await conn.fetch(
        "SELECT s.user_id, s.username, s.role, COALESCE(s.display_full_name, u.full_name) AS full_name "
        "FROM solo_players s JOIN users u ON u.telegram_id = s.user_id "
        "WHERE s.tournament_id = " + _ACTIVE + " ORDER BY s.created_at, s.user_id LIMIT $1 OFFSET $2",
        limit, offset,
    )


async def delete(conn, user_id: int) -> bool:
    return (
        await conn.execute(f"DELETE FROM solo_players WHERE user_id = $1 AND tournament_id = {_ACTIVE}", user_id)
    ) == "DELETE 1"
