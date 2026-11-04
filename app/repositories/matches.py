from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

_SELECT = """
SELECT m.id, m.tournament_id, m.stage_code, s.title AS stage_title, s.sort_order, m.status, m.winner_id,
       m.team_a_id, ta.name AS team_a, m.team_b_id, tb.name AS team_b, tw.name AS winner,
       m.created_at, m.finished_at
  FROM matches m
  JOIN stages s ON s.code = m.stage_code
  JOIN teams ta ON ta.id = m.team_a_id
  JOIN teams tb ON tb.id = m.team_b_id
  LEFT JOIN teams tw ON tw.id = m.winner_id
"""
_ACTIVE = "(SELECT id FROM tournaments WHERE status = 'active')"


async def stages(conn) -> list[dict]:
    return await conn.fetch("SELECT code, title FROM stages ORDER BY sort_order")


async def stage_exists(conn, code: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM stages WHERE code = $1", code))


async def create(conn, stage: str, a: int, b: int) -> int:
    """tournament_id is always the currently active tournament (the eligibility trigger also
    verifies both teams belong to it)."""
    return await conn.fetchval(
        f"INSERT INTO matches (stage_code, team_a_id, team_b_id, tournament_id) VALUES ($1,$2,$3,{_ACTIVE}) RETURNING id",
        stage, a, b,
    )


async def get(conn, match_id: int) -> Optional[dict]:
    return await conn.fetchrow(_SELECT + " WHERE m.id = $1", match_id)


async def delete(conn, match_id: int) -> bool:
    return (await conn.execute("DELETE FROM matches WHERE id = $1", match_id)) == "DELETE 1"


async def count_by_status(conn, status: str) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM matches WHERE status = $1 AND tournament_id = {_ACTIVE}", status)


async def list_by_status(conn, status: str, limit: int, offset: int) -> list[dict]:
    return await conn.fetch(
        _SELECT + f" WHERE m.status = $1 AND m.tournament_id = {_ACTIVE} ORDER BY s.sort_order, m.id LIMIT $2 OFFSET $3",
        status, limit, offset,
    )


async def list_all(conn) -> list[dict]:
    """Bracket of the currently active tournament (public/admin live view)."""
    return await conn.fetch(_SELECT + f" WHERE m.tournament_id = {_ACTIVE} ORDER BY s.sort_order, m.id")


async def list_for_tournament(conn, tournament_id: int) -> list[dict]:
    """Bracket of a specific (typically archived) tournament - used by history."""
    return await conn.fetch(_SELECT + " WHERE m.tournament_id = $1 ORDER BY s.sort_order, m.id", tournament_id)


async def get_by_stage(conn, tournament_id: int, stage_code: str) -> Optional[dict]:
    return await conn.fetchrow(_SELECT + " WHERE m.tournament_id = $1 AND m.stage_code = $2", tournament_id, stage_code)


async def set_winner(conn, match_id: int, winner_id: int) -> None:
    await conn.execute(
        "UPDATE matches SET winner_id = $2, status = 'finished', finished_at = datetime('now') WHERE id = $1",
        match_id, winner_id,
    )
