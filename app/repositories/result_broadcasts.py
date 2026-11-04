from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite


async def create_draft(conn, kind: str, context_id: int, created_by: int, ctype: str, text: Optional[str], file_id: Optional[str]) -> int:
    return await conn.fetchval(
        "INSERT INTO result_broadcasts (kind, context_id, created_by, content_type, text, file_id) "
        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        kind, context_id, created_by, ctype, text, file_id,
    )


async def get(conn, rid: int) -> Optional[dict]:
    return await conn.fetchrow("SELECT * FROM result_broadcasts WHERE id = $1", rid)


async def cancel_draft(conn, rid: int) -> bool:
    return (await conn.execute("UPDATE result_broadcasts SET status='cancelled' WHERE id=$1 AND status='draft'", rid)) == "UPDATE 1"


async def claim(conn, rid: int, total: int) -> Optional[dict]:
    return await conn.fetchrow(
        "UPDATE result_broadcasts SET status='sending', total=$2, started_at=datetime('now') WHERE id=$1 AND status='draft' RETURNING *",
        rid, total,
    )


async def record_delivery(conn, rid: int, recipient_id: int, status: str) -> None:
    await conn.execute(
        "INSERT INTO result_broadcast_deliveries (result_broadcast_id, recipient_id, status) VALUES ($1,$2,$3) "
        "ON CONFLICT (result_broadcast_id, recipient_id) DO UPDATE SET status = EXCLUDED.status, updated_at = datetime('now')",
        rid, recipient_id, status,
    )


async def delivered_ok(conn, rid: int) -> set[int]:
    return {r["recipient_id"] for r in await conn.fetch(
        "SELECT recipient_id FROM result_broadcast_deliveries WHERE result_broadcast_id = $1 AND status = 'sent'", rid
    )}


async def failed_recipients(conn, rid: int) -> list[int]:
    return [r["recipient_id"] for r in await conn.fetch(
        "SELECT recipient_id FROM result_broadcast_deliveries WHERE result_broadcast_id = $1 AND status = 'failed'", rid
    )]


async def finish(conn, rid: int, status: str, sent: int, failed: int, blocked: int) -> None:
    await conn.execute(
        "UPDATE result_broadcasts SET status=$2, sent=$3, failed=$4, blocked=$5, finished_at=datetime('now') WHERE id=$1",
        rid, status, sent, failed, blocked,
    )


async def start_retry(conn, rid: int) -> bool:
    return (await conn.execute("UPDATE result_broadcasts SET status='sending' WHERE id=$1 AND status='done'", rid)) == "UPDATE 1"


async def interrupt_stale(conn) -> int:
    r = await conn.execute("UPDATE result_broadcasts SET status='interrupted', finished_at=datetime('now') WHERE status='sending'")
    return int(r.split()[-1])


# ---- recipients -------------------------------------------------------------------------------
async def _team_members(conn, team_id: Optional[int]) -> list[int]:
    if team_id is None:
        return []
    return [r["user_id"] for r in await conn.fetch(
        "SELECT m.user_id FROM team_members m JOIN users u ON u.telegram_id = m.user_id "
        "WHERE m.team_id = $1 AND NOT u.is_blocked ORDER BY m.user_id",
        team_id,
    )]


async def recipients(conn, kind: str, context_id: int) -> list[int]:
    if kind == "series_winners":
        m = await conn.fetchrow("SELECT winner_id FROM matches WHERE id = $1", context_id)
        return await _team_members(conn, m["winner_id"]) if m else []
    if kind == "series_losers":
        m = await conn.fetchrow("SELECT team_a_id, team_b_id, winner_id FROM matches WHERE id = $1", context_id)
        if not m or not m["winner_id"]:
            return []
        loser = m["team_b_id"] if m["winner_id"] == m["team_a_id"] else m["team_a_id"]
        return await _team_members(conn, loser)
    t = await conn.fetchrow(
        "SELECT first_place_team_id, second_place_team_id, third_place_team_id FROM tournaments WHERE id = $1", context_id
    )
    if not t:
        return []
    if kind == "final_first":
        return await _team_members(conn, t["first_place_team_id"])
    if kind == "final_second":
        return await _team_members(conn, t["second_place_team_id"])
    if kind == "final_third":
        return await _team_members(conn, t["third_place_team_id"])
    top3 = {t["first_place_team_id"], t["second_place_team_id"], t["third_place_team_id"]} - {None}
    rows = await conn.fetch(
        """
        SELECT DISTINCT m.user_id AS uid FROM team_members m JOIN teams tm ON tm.id = m.team_id
         WHERE tm.tournament_id = $1
        UNION
        SELECT user_id AS uid FROM solo_players WHERE tournament_id = $1
        """,
        context_id,
    )
    all_ids = {r["uid"] for r in rows}
    if kind == "final_others":
        excluded = set()
        for tid in top3:
            excluded |= set(await _team_members(conn, tid))
        candidate_ids = all_ids - excluded
    else:  # final_all
        candidate_ids = all_ids
    if not candidate_ids:
        return []
    return [r["telegram_id"] for r in await conn.fetch(
        "SELECT telegram_id FROM users WHERE telegram_id = ANY($1::bigint[]) AND NOT is_blocked ORDER BY telegram_id",
        list(candidate_ids),
    )]
