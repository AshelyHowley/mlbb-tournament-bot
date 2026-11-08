from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite


async def create_draft(conn, admin_id: int, audience: str, ctype: str, text: Optional[str], file_id: Optional[str]) -> int:
    return await conn.fetchval(
        "INSERT INTO broadcasts (admin_id, audience, content_type, text, file_id) VALUES ($1,$2,$3,$4,$5) RETURNING id",
        admin_id, audience, ctype, text, file_id,
    )


async def get(conn, bid: int) -> Optional[dict]:
    return await conn.fetchrow("SELECT * FROM broadcasts WHERE id = $1", bid)


async def claim(conn, bid: int, total: int) -> Optional[dict]:
    """draft -> sending exactly once. Second caller gets None."""
    return await conn.fetchrow(
        "UPDATE broadcasts SET status='sending', total=$2, started_at=datetime('now') WHERE id=$1 AND status='draft' RETURNING *",
        bid, total,
    )


async def cancel_draft(conn, bid: int) -> bool:
    return (await conn.execute("UPDATE broadcasts SET status='cancelled' WHERE id=$1 AND status='draft'", bid)) == "UPDATE 1"


async def progress(conn, bid: int, sent: int, failed: int, blocked: int) -> None:
    await conn.execute("UPDATE broadcasts SET sent=$2, failed=$3, blocked=$4 WHERE id=$1", bid, sent, failed, blocked)


async def finish(conn, bid: int, status: str, sent: int, failed: int, blocked: int) -> None:
    await conn.execute(
        "UPDATE broadcasts SET status=$2, sent=$3, failed=$4, blocked=$5, finished_at=datetime('now') WHERE id=$1 AND status='sending'",
        bid, status, sent, failed, blocked,
    )


async def interrupt_stale(conn) -> int:
    r = await conn.execute("UPDATE broadcasts SET status='interrupted', finished_at=datetime('now') WHERE status='sending'")
    return int(r.split()[-1])


async def recipients(conn, audience: str) -> list[int]:
    active = "(SELECT id FROM tournaments WHERE status = 'active')"
    parts = []
    if audience in ("captains", "all"):
        parts.append(f"SELECT captain_id AS uid FROM teams WHERE tournament_id = {active} AND captain_id IS NOT NULL")
    if audience in ("solo", "all"):
        parts.append(f"SELECT user_id AS uid FROM solo_players WHERE tournament_id = {active}")
    if audience == "all":
        parts.append(f"SELECT m.user_id AS uid FROM team_members m JOIN teams t ON t.id = m.team_id WHERE t.tournament_id = {active}")
    if not parts:
        return []
    sql = "SELECT DISTINCT r.uid FROM (" + " UNION ".join(parts) + ") r JOIN users u ON u.telegram_id = r.uid WHERE COALESCE(u.is_blocked, 0) = 0"
    rows = await conn.fetch(sql)
    return [r["uid"] for r in rows if r["uid"]]
