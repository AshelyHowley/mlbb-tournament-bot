from __future__ import annotations

import logging
import time
from typing import Any, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.repositories import users as users_repo

log = logging.getLogger(__name__)


class UserTrackMiddleware(BaseMiddleware):
    """Keeps `users` fresh (username/last seen) with at most one write per user per interval."""

    def __init__(self, pool, interval: float = 300.0, max_cache: int = 5000) -> None:
        self.pool, self.interval, self.max_cache = pool, interval, max_cache
        self._seen: dict[int, tuple[float, str | None]] = {}

    async def __call__(self, handler: Callable, event: TelegramObject, data: dict[str, Any]):
        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            now = time.monotonic()
            prev = self._seen.get(user.id)
            # /start always refreshes: it is how a user who blocked the bot earlier becomes reachable again
            is_start = bool(getattr(event, "message", None) and (event.message.text or "").startswith("/start"))
            if is_start or prev is None or now - prev[0] > self.interval or prev[1] != user.username:
                try:
                    async with self.pool.acquire() as conn:
                        await users_repo.upsert_user(conn, user.id, user.username, user.full_name)
                    if len(self._seen) >= self.max_cache:
                        self._seen.clear()
                    self._seen[user.id] = (now, user.username)
                except Exception:
                    log.exception("user tracking failed")
        return await handler(event, data)
