from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence

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


async def send_payload(bot: Bot, chat_id: int, p: Payload, reply_markup=None) -> None:
    # parse_mode=None: admin text is delivered verbatim, never interpreted as HTML.
    kw = {"reply_markup": reply_markup} if reply_markup is not None else {}
    if p.content_type == "text":
        await bot.send_message(chat_id, p.text, parse_mode=None, **kw)
    elif p.content_type == "photo":
        await bot.send_photo(chat_id, p.file_id, caption=p.text, parse_mode=None, **kw)
    elif p.content_type == "video":
        await bot.send_video(chat_id, p.file_id, caption=p.text, parse_mode=None, **kw)
    else:
        await bot.send_document(chat_id, p.file_id, caption=p.text, parse_mode=None, **kw)


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
            # the delivery log must not lose a message that already went out, even on shutdown
            # (a resumed broadcast would send it twice otherwise)
            write = asyncio.ensure_future(on_result(uid, outcome))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await write
                raise
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
            count = len(await bc_repo.recipients(conn, audience, exclude=admin_id))
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
                recipients = await bc_repo.recipients(conn, row["audience"], exclude=admin_id)
                claimed = await bc_repo.claim(conn, bid, len(recipients))
        if claimed is None:
            raise Conflict("ℹ️ Эта рассылка уже отправлена или отменена.")
        self._spawn(bid, admin_id, recipients, Payload(row["content_type"], row["text"], row["file_id"]), Stats())
        return len(recipients)

    async def resume(self, bid: int, admin_id: int) -> int:
        """Sends an interrupted broadcast to everyone in its audience (as it is now) who has not been tried yet.
        Returns how many people are left; 0 means everyone was already reached and it is closed as done."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await bc_repo.get(conn, bid)
                if not row or row["admin_id"] != admin_id:
                    raise NotFound("Рассылка")
                if not row["tracked"]:
                    raise Conflict("ℹ️ Эту рассылку нельзя дослать: она запускалась до обновления бота.")
                done = await bc_repo.attempted(conn, bid)
                rest = [u for u in await bc_repo.recipients(conn, row["audience"], exclude=admin_id) if u not in done]
                claimed = await bc_repo.claim_resume(conn, bid, admin_id, len(done) + len(rest))
                base = self._base(await bc_repo.delivery_counts(conn, bid))
                if claimed is not None and not rest:
                    await bc_repo.finish(conn, bid, "done", base.sent, base.failed, base.blocked)
        if claimed is None:
            raise Conflict("ℹ️ Эта рассылка уже досылается или завершена.")
        if rest:
            self._spawn(bid, admin_id, rest, Payload(row["content_type"], row["text"], row["file_id"]), base)
        return len(rest)

    @staticmethod
    def _base(counts: dict[str, int]) -> Stats:
        return Stats(sent=counts.get("ok", 0), failed=counts.get("failed", 0), blocked=counts.get("blocked", 0))

    def _spawn(self, bid: int, admin_id: int, recipients: list[int], payload: Payload, base: Stats) -> None:
        task = asyncio.create_task(self._run(bid, admin_id, recipients, payload, base), name=f"broadcast-{bid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, bid: int, admin_id: int, recipients: list[int], payload: Payload, base: Stats) -> None:
        stats = Stats()

        def total(st: Stats) -> Stats:
            return Stats(base.sent + st.sent, base.failed + st.failed, base.blocked + st.blocked)

        async def progress(st: Stats) -> None:
            t = total(st)
            async with self.pool.acquire() as conn:
                await bc_repo.progress(conn, bid, t.sent, t.failed, t.blocked)

        async def blocked(uid: int) -> None:
            async with self.pool.acquire() as conn:
                await users_repo.mark_blocked(conn, uid)

        async def result(uid: int, outcome: str) -> None:
            async with self.pool.acquire() as conn:
                await bc_repo.record_delivery(conn, bid, uid, outcome)

        try:
            stats = await deliver(
                self.bot, recipients, payload, interval=self.interval,
                on_progress=progress, on_blocked=blocked, on_result=result,
            )
            status = "done"
        except asyncio.CancelledError:
            # shutdown: the sender is told after the restart (notify_interrupted) and offered to resume
            await asyncio.shield(self._finish_from_log(bid))
            raise
        except Exception:
            log.exception("broadcast %s crashed", bid)
            await self._finish_from_log(bid)
            await self._offer_resume(bid, admin_id)
            return
        final = total(stats)
        await self._finish(bid, status, final)
        if status == "done":
            text = (f"📢 Рассылка #{bid} завершена.\n"
                    f"Доставлено: {final.sent}\nБот заблокирован: {final.blocked}\nОшибок: {final.failed}")
            await self._tell(admin_id, text)

    async def _finish_from_log(self, bid: int) -> None:
        """Marks the broadcast interrupted with exact counters taken from the per-recipient log."""
        async with self.pool.acquire() as conn:
            st = self._base(await bc_repo.delivery_counts(conn, bid))
            await bc_repo.finish(conn, bid, "interrupted", st.sent, st.failed, st.blocked)

    async def _tell(self, admin_id: int, text: str, reply_markup=None) -> bool:
        try:
            await self.bot.send_message(admin_id, text, parse_mode=None, reply_markup=reply_markup)
            return True
        except TelegramAPIError:
            log.warning("could not notify admin %s about a broadcast", admin_id)
            return False

    async def _offer_resume(self, bid: int, admin_id: int) -> None:
        from app.keyboards.admin import bc_resume
        async with self.pool.acquire() as conn:
            row = await bc_repo.get(conn, bid)
            st = self._base(await bc_repo.delivery_counts(conn, bid))
        text = (f"📢 Рассылка #{bid} прервана: доставлено {st.sent} из {row['total']}.\n"
                f"Бот заблокирован: {st.blocked}\nОшибок: {st.failed}\n\n"
                "Дослать тем, кому ещё не отправлено? Повторно сообщение никто не получит.")
        if await self._tell(admin_id, text, bc_resume(bid)):
            async with self.pool.acquire() as conn:
                await bc_repo.mark_notified(conn, bid)

    async def notify_interrupted(self) -> int:
        """Called at startup (after recover): tells each sender about broadcasts cut off by a restart."""
        async with self.pool.acquire() as conn:
            rows = await bc_repo.unnotified_interrupted(conn)
        for r in rows:
            await self._offer_resume(r["id"], r["admin_id"])
        return len(rows)

    async def _finish(self, bid: int, status: str, st: Stats) -> None:
        async with self.pool.acquire() as conn:
            await bc_repo.finish(conn, bid, status, st.sent, st.failed, st.blocked)

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
