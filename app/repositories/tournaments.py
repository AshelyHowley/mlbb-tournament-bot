from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

_SELECT = "SELECT id, status, started_at, completed_at, completed_by, first_place_team_id, second_place_team_id, third_place_team_id, created_at FROM tournaments "


async def active_id(conn) -> Optional[int]:
    return await conn.fetchval("SELECT id FROM tournaments WHERE status = 'active'")


async def get_active(conn, lock: bool = False) -> dict:
    return await conn.fetchrow(_SELECT + "WHERE status = 'active'")


async def get(conn, tournament_id: int) -> Optional[dict]:
    return await conn.fetchrow(_SELECT + "WHERE id = $1", tournament_id)


async def history_page(conn, limit: int, offset: int) -> list[dict]:
    return await conn.fetch(
        _SELECT + "WHERE status <> 'active' ORDER BY completed_at DESC NULLS LAST, id DESC LIMIT $1 OFFSET $2",
        limit, offset,
    )


async def history_count(conn) -> int:
    return await conn.fetchval("SELECT count(*) FROM tournaments WHERE status <> 'active'")


async def finalize(conn, tournament_id: int, status: str, completed_by: int, first, second, third) -> None:
    await conn.execute(
        """
        UPDATE tournaments
           SET status = $2, completed_at = datetime('now'), completed_by = $3,
               first_place_team_id = $4, second_place_team_id = $5, third_place_team_id = $6
         WHERE id = $1 AND status = 'active'
        """,
        tournament_id, status, completed_by, first, second, third,
    )


async def create_new_active(conn) -> int:
    return await conn.fetchval("INSERT INTO tournaments (status) VALUES ('active') RETURNING id")


async def snapshot_display(conn, tournament_id: int) -> None:
    """Freeze team_members/solo_players display fields for this tournament - called once, at archive,
    so history keeps showing names/usernames exactly as they were at that moment."""
    await conn.execute(
        """
        UPDATE team_members m SET display_username = u.username, display_full_name = u.full_name
          FROM users u WHERE u.telegram_id = m.user_id AND m.tournament_id = $1
        """,
        tournament_id,
    )
    await conn.execute(
        """
        UPDATE solo_players s SET display_full_name = u.full_name
          FROM users u WHERE u.telegram_id = s.user_id AND s.tournament_id = $1
        """,
        tournament_id,
    )


# ---- read-only aggregates for the history card ----------------------------------------------
async def teams_of(conn, tournament_id: int) -> list[dict]:
    return await conn.fetch(
        "SELECT id, name FROM teams WHERE tournament_id = $1 ORDER BY name_norm, id", tournament_id
    )


async def solo_of(conn, tournament_id: int) -> list[dict]:
    return await conn.fetch(
        """
        SELECT s.user_id, s.role, s.username, COALESCE(s.display_full_name, u.full_name) AS full_name
          FROM solo_players s JOIN users u ON u.telegram_id = s.user_id
         WHERE s.tournament_id = $1 ORDER BY s.created_at, s.user_id
        """,
        tournament_id,
    )


async def stats(conn, tournament_id: int) -> dict:
    return await conn.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM teams WHERE tournament_id = $1) AS teams,
            (SELECT count(*) FROM solo_players WHERE tournament_id = $1) AS solo,
            (SELECT count(*) FROM matches WHERE tournament_id = $1 AND status = 'finished') AS series,
            (SELECT count(*) FROM match_games g JOIN matches m ON m.id = g.match_id WHERE m.tournament_id = $1) AS games
        """,
        tournament_id,
    )


async def bo5_breakdown(conn, tournament_id: int) -> list[dict]:
    return await conn.fetch(
        """
        SELECT greatest(wa, wb) AS win, least(wa, wb) AS lose, count(*) AS n FROM (
            SELECT m.id,
                   (SELECT count(*) FROM match_games g WHERE g.match_id = m.id AND g.winner_team_id = m.team_a_id) wa,
                   (SELECT count(*) FROM match_games g WHERE g.match_id = m.id AND g.winner_team_id = m.team_b_id) wb
              FROM matches m WHERE m.tournament_id = $1 AND m.status = 'finished'
        ) s GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC
        """,
        tournament_id,
    )
