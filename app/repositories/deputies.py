from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite


async def create_invite(conn, created_by: int, token: str) -> int:
    return await conn.fetchval(
        "INSERT INTO deputy_invites (created_by, token) VALUES ($1, $2) RETURNING id", created_by, token
    )


async def get_invite_by_token(conn, token: str, lock: bool = False) -> Optional[dict]:
    return await conn.fetchrow(
        "SELECT * FROM deputy_invites WHERE token = $1" + (" FOR UPDATE" if lock else ""), token
    )


async def get_invite(conn, invite_id: int, lock: bool = False) -> Optional[dict]:
    return await conn.fetchrow(
        "SELECT * FROM deputy_invites WHERE id = $1" + (" FOR UPDATE" if lock else ""), invite_id
    )


async def claim_invite(conn, token: str, candidate_id: int) -> Optional[dict]:
    """Atomic CAS: only the first concurrent claim succeeds. One token -> one candidate."""
    return await conn.fetchrow(
        "UPDATE deputy_invites SET status='claimed', candidate_id=$2, claimed_at=datetime('now') "
        "WHERE token=$1 AND status='open' RETURNING *",
        token, candidate_id,
    )


async def decide_invite(conn, invite_id: int, status: str, decided_by: int) -> bool:
    return (
        await conn.execute(
            "UPDATE deputy_invites SET status=$2, decided_at=datetime('now'), decided_by=$3 WHERE id=$1 AND status='claimed'",
            invite_id, status, decided_by,
        )
    ) == "UPDATE 1"


async def pending_invites(conn) -> list[dict]:
    return await conn.fetch(
        """
        SELECT di.id, di.candidate_id, u.username, u.full_name
          FROM deputy_invites di JOIN users u ON u.telegram_id = di.candidate_id
         WHERE di.status = 'claimed' ORDER BY di.claimed_at
        """
    )


async def add_deputy(conn, user_id: int, invite_id: Optional[int], approved_by: int) -> None:
    await conn.execute(
        "INSERT INTO deputies (user_id, invite_id, approved_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id) DO UPDATE SET invite_id = EXCLUDED.invite_id, approved_by = EXCLUDED.approved_by, "
        "approved_at = datetime('now'), revoked_at = NULL, revoked_by = NULL",
        user_id, invite_id, approved_by,
    )


async def revoke(conn, user_id: int, revoked_by: int) -> bool:
    return (
        await conn.execute(
            "UPDATE deputies SET revoked_at = datetime('now'), revoked_by = $2 WHERE user_id = $1 AND revoked_at IS NULL",
            user_id, revoked_by,
        )
    ) == "UPDATE 1"


async def is_active_deputy(conn, user_id: int) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM deputies WHERE user_id = $1 AND revoked_at IS NULL", user_id))


async def active_deputies(conn) -> list[dict]:
    return await conn.fetch(
        """
        SELECT d.user_id, d.approved_at, u.username, u.full_name
          FROM deputies d JOIN users u ON u.telegram_id = d.user_id
         WHERE d.revoked_at IS NULL ORDER BY d.approved_at
        """
    )


async def active_count(conn) -> int:
    return await conn.fetchval("SELECT count(*) FROM deputies WHERE revoked_at IS NULL")
