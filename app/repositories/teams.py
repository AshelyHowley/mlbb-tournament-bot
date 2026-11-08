from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

_TEAM_SQL = (
    "SELECT t.id, t.name, t.invite_token, t.captain_invite_token, t.captain_id, t.created_by, t.created_at, "
    "t.tournament_id, cu.username AS creator_username, cu.full_name AS creator_name "
    "FROM teams t LEFT JOIN users cu ON cu.telegram_id = t.created_by "
)
_ACTIVE = "(SELECT id FROM tournaments WHERE status = 'active')"


async def lock_user(conn, user_id: int) -> None:
    """No-op on SQLite (no advisory locks)."""
    return None


async def insert_team(
    conn, name: str, name_norm: str, created_by: int, captain_id: Optional[int], token: str, captain_token: Optional[str], stars: int = 0
) -> int:
    # Ensure active tournament exists
    tid = await conn.fetchval("SELECT id FROM tournaments WHERE status = 'active' LIMIT 1")
    if tid is None:
        await conn.execute("INSERT INTO tournaments (status) VALUES ('active')")
        tid = await conn.fetchval("SELECT id FROM tournaments WHERE status = 'active' LIMIT 1")
    return await conn.fetchval(
        "INSERT INTO teams (name, name_norm, created_by, captain_id, invite_token, captain_invite_token, tournament_id, stars) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
        name, name_norm, created_by, captain_id, token, captain_token, tid, stars,
    )


async def add_member(conn, team_id: int, user_id: int, is_captain: bool = False, is_substitute: bool = False) -> None:
    await conn.execute(
        "INSERT INTO team_members (team_id, user_id, is_captain, is_substitute) VALUES ($1,$2,$3,$4)",
        team_id, user_id, is_captain, 1 if is_substitute else 0,
    )


async def get(conn, team_id: int) -> Optional[dict]:
    return await conn.fetchrow(_TEAM_SQL + "WHERE t.id = $1", team_id)


def _lock(lock: bool) -> str:
    return ""  # SQLite has no FOR UPDATE


async def get_by_token(conn, token: str, lock: bool = False) -> Optional[dict]:
    """FOR UPDATE serialises concurrent joins of the same team on its row."""
    return await conn.fetchrow(_TEAM_SQL + "WHERE t.invite_token = $1" + _lock(lock), token)


async def get_by_captain_token(conn, token: str, lock: bool = False) -> Optional[dict]:
    return await conn.fetchrow(_TEAM_SQL + "WHERE t.captain_invite_token = $1" + _lock(lock), token)


async def get_by_captain(conn, captain_id: int, lock: bool = False) -> Optional[dict]:
    """The team this user currently captains, in the ACTIVE tournament only (a captain_id from an
    archived tournament must not be found here - that duty ended when the tournament was archived)."""
    return await conn.fetchrow(
        _TEAM_SQL + f"WHERE t.captain_id = $1 AND t.tournament_id = {_ACTIVE}" + _lock(lock), captain_id
    )


async def get_created_by(conn, user_id: int, pending_only: bool = False, lock: bool = False) -> Optional[dict]:
    """The team this user registered in the ACTIVE tournament (an ordinary user has at most one)."""
    where = f"WHERE t.created_by = $1 AND t.tournament_id = {_ACTIVE}" + (" AND t.captain_id IS NULL" if pending_only else "")
    return await conn.fetchrow(_TEAM_SQL + where + " ORDER BY t.id DESC LIMIT 1" + _lock(lock), user_id)


async def get_by_name_norm(conn, name_norm: str) -> Optional[dict]:
    return await conn.fetchrow(f"SELECT id FROM teams WHERE name_norm = $1 AND tournament_id = {_ACTIVE}", name_norm)


async def created_count(conn, user_id: int) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM teams WHERE created_by = $1 AND tournament_id = {_ACTIVE}", user_id)


async def membership(conn, user_id: int) -> Optional[dict]:
    """The team a user plays for in the ACTIVE tournament (at most one - UNIQUE(tournament_id, user_id))."""
    return await conn.fetchrow(
        "SELECT m.team_id, m.is_captain, t.name FROM team_members m JOIN teams t ON t.id = m.team_id "
        "WHERE m.user_id = $1 AND t.tournament_id = " + _ACTIVE,
        user_id,
    )


async def members(conn, team_id: int) -> list[dict]:
    """Display name/username: the frozen snapshot once archived, otherwise the live `users` row."""
    return await conn.fetch(
        """
        SELECT m.user_id, m.is_captain, m.is_substitute, m.joined_at,
               COALESCE(m.display_username, u.username) AS username,
               COALESCE(m.display_full_name, u.full_name) AS full_name
          FROM team_members m JOIN users u ON u.telegram_id = m.user_id
         WHERE m.team_id = $1
         ORDER BY m.is_captain DESC, m.joined_at, m.id
        """,
        team_id,
    )


async def member_count(conn, team_id: int) -> int:
    return await conn.fetchval("SELECT count(*) FROM team_members WHERE team_id = $1", team_id)


async def remove_member(conn, team_id: int, user_id: int) -> bool:
    """Never removes the captain row."""
    return (
        await conn.execute(
            "DELETE FROM team_members WHERE team_id = $1 AND user_id = $2 AND NOT is_captain", team_id, user_id
        )
    ) == "DELETE 1"


async def assign_captain(conn, team_id: int, user_id: int) -> bool:
    """Atomic 'captain still pending -> user' (the caller holds the team row lock)."""
    return (
        await conn.execute("UPDATE teams SET captain_id = $2 WHERE id = $1 AND captain_id IS NULL", team_id, user_id)
    ) == "UPDATE 1"


async def flag_member_captain(conn, team_id: int, user_id: int, value: bool) -> bool:
    return (
        await conn.execute(
            "UPDATE team_members SET is_captain = $3 WHERE team_id = $1 AND user_id = $2 AND is_captain <> $3",
            team_id, user_id, value,
        )
    ) == "UPDATE 1"


async def swap_captain(conn, team_id: int, old_id: int, new_id: int) -> bool:
    """Old captain -> ordinary member, member -> captain. Roster size does not change. Team row must be locked."""
    ok = await flag_member_captain(conn, team_id, old_id, False)
    ok = ok and await flag_member_captain(conn, team_id, new_id, True)
    ok = ok and (
        await conn.execute("UPDATE teams SET captain_id = $3 WHERE id = $1 AND captain_id = $2", team_id, old_id, new_id)
    ) == "UPDATE 1"
    return ok


async def set_captain_token(conn, team_id: int, token: str) -> bool:
    return (
        await conn.execute(
            "UPDATE teams SET captain_invite_token = $2 WHERE id = $1 AND captain_id IS NULL", team_id, token
        )
    ) == "UPDATE 1"


async def count(conn) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM teams t WHERE t.tournament_id = {_ACTIVE} ")


async def list_page(conn, limit: int, offset: int) -> list[dict]:
    return await conn.fetch(
        f"SELECT t.id, t.name FROM teams t WHERE t.tournament_id = {_ACTIVE}  ORDER BY t.created_at, t.id LIMIT $1 OFFSET $2",
        limit, offset,
    )


async def list_all(conn) -> list[dict]:
    return await conn.fetch(f"SELECT id, name FROM teams WHERE tournament_id = {_ACTIVE} ORDER BY name_norm, id")


async def delete(conn, team_id: int) -> bool:
    return (await conn.execute("DELETE FROM teams WHERE id = $1", team_id)) == "DELETE 1"
