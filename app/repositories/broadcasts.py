from __future__ import annotations

from typing import Optional



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


async def recipients(conn, audience: str, exclude: Optional[int] = None) -> list[int]:
    """captains: each team's captain, or its registrar while the team has no captain.
    solo: solo players not yet assembled into a team (those are reached as team players).
    all: every team player, every registrar and every solo player.
    `exclude` (the sending admin) never receives their own broadcast."""
    active = "(SELECT id FROM tournaments WHERE status = 'active')"
    parts = []
    if audience == "captains":
        parts.append(f"SELECT COALESCE(captain_id, created_by) AS uid FROM teams WHERE tournament_id = {active}")
    if audience == "solo":
        parts.append(
            f"SELECT s.user_id AS uid FROM solo_players s WHERE s.tournament_id = {active} AND NOT EXISTS ("
            "SELECT 1 FROM team_members m JOIN teams t ON t.id = m.team_id "
            f"WHERE m.user_id = s.user_id AND t.from_solo = 1 AND t.tournament_id = {active})"
        )
    if audience == "all":
        parts.append(f"SELECT user_id AS uid FROM solo_players WHERE tournament_id = {active}")
        parts.append(f"SELECT m.user_id AS uid FROM team_members m JOIN teams t ON t.id = m.team_id WHERE t.tournament_id = {active}")
        parts.append(f"SELECT created_by AS uid FROM teams WHERE tournament_id = {active}")
    if not parts:
        return []
    sql = ("SELECT DISTINCT r.uid FROM (" + " UNION ".join(parts) + ") r JOIN users u ON u.telegram_id = r.uid "
           "WHERE COALESCE(u.is_blocked, 0) = 0 AND r.uid IS NOT NULL AND r.uid IS NOT $1 ORDER BY r.uid")
    rows = await conn.fetch(sql, exclude)
    return [r["uid"] for r in rows]
