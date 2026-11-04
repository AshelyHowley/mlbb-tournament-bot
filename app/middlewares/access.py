"""Global access gate. Registered as the first *outer update middleware* we own, so a user who is not
allowed never reaches filters, FSM handlers, business logic or any database query."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update

from app.config import Config

log = logging.getLogger(__name__)

DENIED_TEXT = (
    "🔒 Доступ к боту ограничен\n\n"
    "Сейчас бот находится в режиме закрытого тестирования.\n\n"
    "У вашего аккаунта нет доступа к данному боту."
)
DENIED_ALERT = "🔒 Доступ к боту ограничен\n\nСейчас бот находится в режиме закрытого тестирования."


class AccessMiddleware(BaseMiddleware):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        allowed = user is not None and not user.is_bot and self.cfg.can_access_bot(user.id)
        if self.cfg.access_mode == "public" and user is None:
            allowed = True  # nothing to authorise (e.g. channel posts); no handler reacts to them

        if not allowed:
            await self._deny(event)
            return None

        # The bot lives in private chats only: it must never answer inside the tournament chat/channel.
        if isinstance(event, Update) and event.message and event.message.chat.type != "private":
            return None
        return await handler(event, data)

    @staticmethod
    async def _deny(event: TelegramObject) -> None:
        if not isinstance(event, Update):
            return
        try:
            if event.callback_query:
                await event.callback_query.answer(DENIED_ALERT, show_alert=True)  # always stop the spinner
            elif event.message and event.message.chat.type == "private":
                await event.message.answer(DENIED_TEXT, parse_mode=None)
            # every other update type is dropped silently
        except TelegramAPIError:
            log.debug("could not deliver access-denied notice")
        uid = None
        for part in (event.message, event.callback_query):
            if part is not None and part.from_user is not None:
                uid = part.from_user.id
        log.info("access denied (user id %s)", uid)
