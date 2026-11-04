from __future__ import annotations

# # import asyncpg  # removed for SQLite  # removed for SQLite


async def lock_match(conn, match_id: int) -> None:
    """Serialises every game submission for this series (also the row the DB trigger itself locks)."""
    await conn.execute("SELECT 1 FROM matches WHERE id = $1 FOR UPDATE", match_id)


async def game_count(conn, match_id: int) -> int:
    return await conn.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", match_id)


async def games(conn, match_id: int) -> list[dict]:
    return await conn.fetch(
        """
        SELECT g.id, g.game_number, g.winner_team_id, t.name AS winner_name, g.created_by, g.created_at, g.updated_at
          FROM match_games g JOIN teams t ON t.id = g.winner_team_id
         WHERE g.match_id = $1 ORDER BY g.game_number
        """,
        match_id,
    )


async def add_game(conn, match_id: int, winner_team_id: int, created_by: int) -> int:
    """game_number is assigned authoritatively by the DB trigger (the '1' below is only a placeholder)."""
    return await conn.fetchval(
        "INSERT INTO match_games (match_id, game_number, winner_team_id, created_by) VALUES ($1, 1, $2, $3) RETURNING id",
        match_id, winner_team_id, created_by,
    )


async def edit_game(conn, game_id: int, winner_team_id: int, updated_by: int) -> bool:
    return (
        await conn.execute(
            "UPDATE match_games SET winner_team_id = $2, updated_by = $3 WHERE id = $1", game_id, winner_team_id, updated_by
        )
    ) == "UPDATE 1"


async def get_game(conn, game_id: int) -> dict | None:
    return await conn.fetchrow("SELECT * FROM match_games WHERE id = $1", game_id)


async def other_matches_involving(conn, tournament_id: int, exclude_match_id: int, team_a: int, team_b: int) -> int:
    """How many OTHER matches (any status) of this tournament already feature either team -
    used to decide whether a completed series may still be corrected."""
    return await conn.fetchval(
        """
        SELECT count(*) FROM matches
         WHERE tournament_id = $1 AND id <> $2 AND (team_a_id IN ($3, $4) OR team_b_id IN ($3, $4))
        """,
        tournament_id, exclude_match_id, team_a, team_b,
    )
