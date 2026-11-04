from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence

# # import asyncpg  # removed for SQLite  # removed for SQLite
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.repositories import broadcasts as bc_repo
from app.repositories import users as users_repo
from app.services.errors import Conflict, NotFound, ValidationError

log = logging.getLogger(__name__)

AUDIENCES = {"captains": "👑 Капитаны", "solo": "👤 Соло", "all": "👥 Все доступные участники"}
MAX_TEXT, MAX_CAPTION = 4096, 1024
SEND_INTERVAL = 0.05  # ~20 msg/s, below Telegram's ~30 msg/s broadcast limit
MAX_ATTEMPTS = 3
PROGRESS_EVERY = 20


@dataclass(frozen=True)
class Payload:
    content_type: str  # text | photo | video | document
    text: Optional[str]
    file_id: Optional[str] = None


@dataclass
class Stats:
    sent: int = 0
    failed: int = 0
    blocked: int = 0


def validate_payload(p: Payload) -> None:
    if p.content_type == "text":
        if not p.text or not p.text.strip():
            raise ValidationError("❌ Пустой текст.")
        if len(p.text) > MAX_TEXT:
            raise ValidationError(f"❌ Текст длиннее {MAX_TEXT} символов.")
    elif p.content_type in ("photo", "video", "document"):
        if not p.file_id:
            raise ValidationError("❌ Нет файла.")
        if p.text and len(p.text) > MAX_CAPTION:
            raise ValidationError(f"❌ Подпись длиннее {MAX_CAPTION} символов.")
    else:
        raise ValidationError("❌ Поддерживаются текст, фото, видео и документ.")


async def send_payload(bot: Bot, chat_id: int, p: Payload) -> None:
    # parse_mode=None: admin text is delivered verbatim, never interpreted as HTML.
    if p.content_type == "text":
        await bot.send_message(chat_id, p.text, parse_mode=None)
    elif p.content_type == "photo":
        await bot.send_photo(chat_id, p.file_id, caption=p.text, parse_mode=None)
    elif p.content_type == "video":
        await bot.send_video(chat_id, p.file_id, caption=p.text, parse_mode=None)
    else:
        await bot.send_document(chat_id, p.file_id, caption=p.text, parse_mode=None)


async def deliver(
    bot: Bot,
    recipients: Sequence[int],
    payload: Payload,
    *,
    interval: float = SEND_INTERVAL,
    on_progress: Optional[Callable[[Stats], Awaitable[None]]] = None,
    on_blocked: Optional[Callable[[int], Awaitable[None]]] = None,
    on_result: Optional[Callable[[int, str], Awaitable[None]]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Stats:
    """Send to everyone; one failing recipient never stops the rest. `on_result(uid, outcome)` - outcome
    is 'ok'/'blocked'/'failed' - fires for every recipient, in order; used to persist a per-recipient
    delivery log for later retry (the general broadcast does not need this, so it stays unused there)."""
    st = Stats()
    for i, uid in enumerate(recipients, 1):
        outcome = await _send_with_retries(bot, uid, payload, sleep)
        if outcome == "ok":
            st.sent += 1
        elif outcome == "blocked":
            st.blocked += 1
            if on_blocked:
                await on_blocked(uid)
        else:
            st.failed += 1
        if on_result:
            await on_result(uid, outcome)
        if on_progress and i % PROGRESS_EVERY == 0:
            await on_progress(st)
        if interval:
            await sleep(interval)
    return st


async def _send_with_retries(bot: Bot, uid: int, payload: Payload, sleep) -> str:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await send_payload(bot, uid, payload)
            return "ok"
        except TelegramRetryAfter as e:
            log.warning("RetryAfter %ss (attempt %d)", e.retry_after, attempt)
            await sleep(min(e.retry_after, 60) + 0.5)
        except TelegramForbiddenError:
            return "blocked"
        except (TelegramNetworkError, TelegramServerError):
            await sleep(attempt)
        except TelegramAPIError as e:  # BadRequest: chat not found, deactivated, etc.
            log.info("send to %s failed: %s", uid, type(e).__name__)
            return "failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unexpected error sending to %s", uid)
            return "failed"
    return "failed"


class BroadcastService:
    def __init__(self, pool: object, bot: Bot, *, interval: float = SEND_INTERVAL):
        self.pool, self.bot, self.interval = pool, bot, interval
        self._tasks: set[asyncio.Task] = set()

    async def recover(self) -> int:
        """Called at startup: broadcasts left in 'sending' by a crash are marked interrupted."""
        async with self.pool.acquire() as conn:
            return await bc_repo.interrupt_stale(conn)

    async def prepare(self, admin_id: int, audience: str, payload: Payload) -> tuple[int, int]:
        if audience not in AUDIENCES:
            raise ValidationError("❌ Неизвестная аудитория.")
        validate_payload(payload)
        async with self.pool.acquire() as conn:
            bid = await bc_repo.create_draft(conn, admin_id, audience, payload.content_type, payload.text, payload.file_id)
            count = len(await bc_repo.recipients(conn, audience))
        return bid, count

    async def get(self, bid: int):
        async with self.pool.acquire() as conn:
            row = await bc_repo.get(conn, bid)
        if not row:
            raise NotFound("Рассылка")
        return row

    async def cancel(self, bid: int) -> bool:
        async with self.pool.acquire() as conn:
            return await bc_repo.cancel_draft(conn, bid)

    async def start(self, bid: int, admin_id: int) -> int:
        """Atomically claims the draft (double-click safe) and runs delivery in the background."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await bc_repo.get(conn, bid)
                if not row or row["admin_id"] != admin_id:
                    raise NotFound("Рассылка")
                recipients = await bc_repo.recipients(conn, row["audience"])
                claimed = await bc_repo.claim(conn, bid, len(recipients))
        if claimed is None:
            raise Conflict("ℹ️ Эта рассылка уже отправлена или отменена.")
        payload = Payload(row["content_type"], row["text"], row["file_id"])
        task = asyncio.create_task(self._run(bid, admin_id, recipients, payload), name=f"broadcast-{bid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return len(recipients)

    async def _run(self, bid: int, admin_id: int, recipients: list[int], payload: Payload) -> None:
        stats = Stats()

        async def progress(st: Stats) -> None:
            async with self.pool.acquire() as conn:
                await bc_repo.progress(conn, bid, st.sent, st.failed, st.blocked)

        async def blocked(uid: int) -> None:
            async with self.pool.acquire() as conn:
                await users_repo.mark_blocked(conn, uid)

        try:
            stats = await deliver(
                self.bot, recipients, payload, interval=self.interval, on_progress=progress, on_blocked=blocked
            )
            status = "done"
        except asyncio.CancelledError:
            status = "interrupted"
            await asyncio.shield(self._finish(bid, status, stats))
            raise
        except Exception:
            log.exception("broadcast %s crashed", bid)
            status = "interrupted"
        await self._finish(bid, status, stats)
        try:
            await self.bot.send_message(
                admin_id,
                f"📢 Рассылка #{bid} {'завершена' if status == 'done' else 'прервана'}.\n"
                f"Доставлено: {stats.sent}\nБот заблокирован: {stats.blocked}\nОшибок: {stats.failed}",
                parse_mode=None,
            )
        except TelegramAPIError:
            log.warning("could not notify admin about broadcast %s", bid)

    async def _finish(self, bid: int, status: str, st: Stats) -> None:
        async with self.pool.acquire() as conn:
            await bc_repo.finish(conn, bid, status, st.sent, st.failed, st.blocked)

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
