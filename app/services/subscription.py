from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Union

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

_OK_STATUSES = {"creator", "administrator", "member"}
_TIMEOUT = 10.0


@dataclass(frozen=True)
class SubscriptionResult:
    channel_ok: bool
    chat_ok: bool
    error: bool = False  # True when Telegram could not answer (never treated as subscribed)

    @property
    def ok(self) -> bool:
        return self.channel_ok and self.chat_ok and not self.error


def is_member_status(member) -> bool:
    status = getattr(member, "status", None)
    if status in _OK_STATUSES:
        return True
    # 'restricted' users are still in the chat unless they have left
    return status == "restricted" and bool(getattr(member, "is_member", False))


class SubscriptionService:
    def __init__(self, bot: Bot, channel_id: Union[str, int], chat_id: Union[str, int]):
        self.bot, self.channel_id, self.chat_id = bot, channel_id, chat_id

    async def _is_in(self, chat_id, user_id: int) -> bool:
        member = await asyncio.wait_for(self.bot.get_chat_member(chat_id, user_id), _TIMEOUT)
        return is_member_status(member)

    async def check(self, user_id: int) -> SubscriptionResult:
        results = await asyncio.gather(
            self._is_in(self.channel_id, user_id),
            self._is_in(self.chat_id, user_id),
            return_exceptions=True,
        )
        error = False
        flags: list[bool] = []
        for r in results:
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, (TelegramAPIError, asyncio.TimeoutError, OSError)):
                    log.warning("getChatMember failed: %s", type(r).__name__)
                else:
                    log.error("getChatMember unexpected error: %r", r)
                error = True
                flags.append(False)
            else:
                flags.append(r)
        return SubscriptionResult(channel_ok=flags[0], chat_ok=flags[1], error=error)
