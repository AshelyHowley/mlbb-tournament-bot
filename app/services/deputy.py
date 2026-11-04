"""Deputy appointment: a secure one-candidate invite link, then explicit MAIN ADMIN approval.
At most 2 active deputies, enforced by PostgreSQL under an advisory lock (never just in Python)."""
from __future__ import annotations

from dataclasses import dataclass

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.repositories import deputies as deputies_repo
from app.repositories import users as users_repo
from app.services.errors import DeputyInviteClaimed, DeputyLimitReached, NotFound
from app.services.registration import TgUser
from app.utils.text import INVITE_TOKEN_RE, new_invite_token

_DEPUTY_LIMIT = "MB008"


@dataclass(frozen=True)
class InvitePeek:
    status: str  # ok | invalid | claimed_by_other | mine


class DeputyService:
    def __init__(self, pool: object):
        self.pool = pool

    async def create_invite(self, created_by: int) -> str:
        async with self.pool.acquire() as conn:
            if await deputies_repo.active_count(conn) >= 2:
                raise DeputyLimitReached()
            token = new_invite_token()
            await deputies_repo.create_invite(conn, created_by, token)
        return token

    async def peek(self, token: str, user_id: int) -> InvitePeek:
        if not INVITE_TOKEN_RE.fullmatch(token or ""):
            return InvitePeek("invalid")
        async with self.pool.acquire() as conn:
            inv = await deputies_repo.get_invite_by_token(conn, token)
        if not inv:
            return InvitePeek("invalid")
        if inv["status"] == "open":
            return InvitePeek("ok")
        if inv["candidate_id"] == user_id:
            return InvitePeek("mine")
        return InvitePeek("claimed_by_other")

    async def claim(self, user: TgUser, token: str) -> dict:
        """First successful claimant locks the token to their user_id (atomic UPDATE...WHERE)."""
        if not INVITE_TOKEN_RE.fullmatch(token or ""):
            raise NotFound("Приглашение")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await users_repo.upsert_user(conn, user.id, user.username, user.full_name)
                claimed = await deputies_repo.claim_invite(conn, token, user.id)
                if claimed:
                    return claimed
                existing = await deputies_repo.get_invite_by_token(conn, token)
                if not existing:
                    raise NotFound("Приглашение")
                if existing["candidate_id"] == user.id:
                    return existing  # idempotent: same candidate re-clicking
                raise DeputyInviteClaimed()

    async def pending(self):
        async with self.pool.acquire() as conn:
            return await deputies_repo.pending_invites(conn)

    async def approve(self, main_admin_id: int, invite_id: int) -> dict:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inv = await deputies_repo.get_invite(conn, invite_id, lock=True)
                if not inv or inv["status"] != "claimed":
                    raise NotFound("Заявка")
                try:
                    await deputies_repo.add_deputy(conn, inv["candidate_id"], invite_id, main_admin_id)
                except asyncpg.PostgresError as e:
                    if e.sqlstate == _DEPUTY_LIMIT:
                        raise DeputyLimitReached()
                    raise
                await deputies_repo.decide_invite(conn, invite_id, "approved", main_admin_id)
            return await deputies_repo.get_invite(conn, invite_id)

    async def reject(self, main_admin_id: int, invite_id: int) -> None:
        async with self.pool.acquire() as conn:
            if not await deputies_repo.decide_invite(conn, invite_id, "rejected", main_admin_id):
                raise NotFound("Заявка")

    async def list_active(self):
        async with self.pool.acquire() as conn:
            return await deputies_repo.active_deputies(conn)

    async def revoke(self, main_admin_id: int, deputy_id: int) -> None:
        async with self.pool.acquire() as conn:
            if not await deputies_repo.revoke(conn, deputy_id, main_admin_id):
                raise NotFound("Заместитель")
