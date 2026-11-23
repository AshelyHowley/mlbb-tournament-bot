from __future__ import annotations

from typing import Optional



async def create_draft(conn, admin_id: int, audience: str, ctype: str, text: Optional[str], file_id: Optional[str],
                       media: Optional[str] = None) -> int:
    return await conn.fetchval(
        "INSERT INTO broadcasts (admin_id, audience, content_type, text, file_id, media) "
        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        admin_id, audience, ctype, text, file_id, media,
    )


async def get(conn, bid: int) -> Optional[dict]:
    return await conn.fetchrow("SELECT * FROM broadcasts WHERE id = $1", bid)


async def claim(conn, bid: int, total: int) -> Optional[dict]:
    """draft -> sending exactly once. Second caller gets None."""
    return await conn.fetchrow(
        "UPDATE broadcasts SET status='sending', total=$2, tracked=1, started_at=datetime('now') "
        "WHERE id=$1 AND status='draft' RETURNING *",
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


async def claim_resume(conn, bid: int, admin_id: int, total: int) -> Optional[dict]:
    """interrupted -> sending exactly once, only for the sender and only if deliveries were logged."""
    return await conn.fetchrow(
        "UPDATE broadcasts SET status='sending', total=$3, notified=1, finished_at=NULL "
        "WHERE id=$1 AND admin_id=$2 AND status='interrupted' AND tracked=1 RETURNING *",
        bid, admin_id, total,
    )


async def record_delivery(conn, bid: int, uid: int, outcome: str) -> None:
    await conn.execute(
        "INSERT INTO broadcast_deliveries (broadcast_id, recipient_id, outcome) VALUES ($1,$2,$3) "
        "ON CONFLICT (broadcast_id, recipient_id) DO UPDATE SET outcome = excluded.outcome",
        bid, uid, outcome,
    )


async def attempted(conn, bid: int) -> set[int]:
    rows = await conn.fetch("SELECT recipient_id FROM broadcast_deliveries WHERE broadcast_id = $1", bid)
    return {r["recipient_id"] for r in rows}


async def delivery_counts(conn, bid: int) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT outcome, COUNT(*) AS n FROM broadcast_deliveries WHERE broadcast_id = $1 GROUP BY outcome", bid
    )
    return {r["outcome"]: r["n"] for r in rows}


async def unnotified_interrupted(conn) -> list[dict]:
    return await conn.fetch(
        "SELECT * FROM broadcasts WHERE status='interrupted' AND tracked=1 AND notified=0 ORDER BY id"
    )


async def mark_notified(conn, bid: int) -> None:
    await conn.execute("UPDATE broadcasts SET notified=1 WHERE id=$1", bid)


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
