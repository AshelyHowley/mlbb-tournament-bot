"""Per-user flood limit: more than ``limit`` updates (messages + button presses) in ``window`` seconds
are dropped. The first dropped update gets one short notice; the rest are ignored silently (a button
press still gets an empty answer so its spinner stops). Staff are never limited; whether someone is a
deputy is looked up only once they actually go over the limit, and cached for a minute."""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update

log = logging.getLogger(__name__)

NOTICE = "⏳ Слишком много действий подряд, подождите несколько секунд."
STAFF_CACHE_SECONDS = 60.0


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, roles, limit: int = 20, window: float = 10.0, clock=time.monotonic, max_users: int = 10000) -> None:
        self.roles, self.limit, self.window, self.clock, self.max_users = roles, limit, window, clock, max_users
        self._hits: dict[int, deque] = {}
        self._warned: set[int] = set()
        self._staff: dict[int, tuple[float, bool]] = {}

    async def _is_staff(self, uid: int, now: float) -> bool:
        if self.roles.is_main_admin(uid):
            return True
        cached = self._staff.get(uid)
        if cached and now - cached[0] < STAFF_CACHE_SECONDS:
            return cached[1]
        try:
            staff = bool(await self.roles.is_staff(uid))
        except Exception:
            log.exception("staff check failed in throttle; treating as regular user")
            staff = False
        self._staff[uid] = (now, staff)
        return staff

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        now = self.clock()
        hits = self._hits.get(user.id)
        if hits is None:
            if len(self._hits) >= self.max_users:
                self._hits.clear()
                self._warned.clear()
                self._staff.clear()
            hits = self._hits[user.id] = deque()
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        hits.append(now)
        if len(hits) <= self.limit:
            self._warned.discard(user.id)
            return await handler(event, data)
        if await self._is_staff(user.id, now):
            return await handler(event, data)
        first = user.id not in self._warned
        self._warned.add(user.id)
        if first:
            log.warning("flood limit hit by user %s", user.id)
        await self._notify(event, first)
        return None

    @staticmethod
    async def _notify(event: TelegramObject, first: bool) -> None:
        if not isinstance(event, Update):
            return
        try:
            if event.callback_query:
                if first:
                    await event.callback_query.answer(NOTICE, show_alert=False)
                else:
                    await event.callback_query.answer()
            elif first and event.message and event.message.chat.type == "private":
                await event.message.answer(NOTICE, parse_mode=None)
        except TelegramAPIError:
            log.debug("could not deliver flood notice")
