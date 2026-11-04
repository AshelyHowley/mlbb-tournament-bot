"""Targeted, staff-composed notifications: a custom series win/loss message, or the tournament's
final placement broadcast. A small sibling of BroadcastService (same draft->sending atomic claim,
same retry-safe delivery loop) that never touches the general broadcast feature or its table."""
from __future__ import annotations

import asyncio
import logging

# # import asyncpg  # removed for SQLite  # removed for SQLite
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.repositories import result_broadcasts as rb_repo
from app.repositories import users as users_repo
from app.services.broadcast import Payload, deliver, validate_payload
from app.services.errors import Conflict, NotFound, ValidationError

log = logging.getLogger(__name__)

KINDS = {
    "series_winners": "🏆 Победители серии",
    "series_losers": "❌ Проигравшие серии",
    "final_first": "👑 1 место",
    "final_second": "🥈 2 место",
    "final_third": "🥉 3 место",
    "final_others": "🎮 Остальные участники",
    "final_all": "👥 Все участники турнира",
}


class ResultNotifyService:
    def __init__(self, pool: object, bot: Bot, *, interval: float = 0.05):
        self.pool, self.bot, self.interval = pool, bot, interval
        self._tasks: set[asyncio.Task] = set()

    async def recover(self) -> int:
        async with self.pool.acquire() as conn:
            return await rb_repo.interrupt_stale(conn)

    async def prepare(self, created_by: int, kind: str, context_id: int, payload: Payload) -> tuple[int, int]:
        if kind not in KINDS:
            raise ValidationError("❌ Неизвестная аудитория.")
        validate_payload(payload)
        async with self.pool.acquire() as conn:
            rid = await rb_repo.create_draft(conn, kind, context_id, created_by, payload.content_type, payload.text, payload.file_id)
            count = len(await rb_repo.recipients(conn, kind, context_id))
        return rid, count

    async def get(self, rid: int):
        async with self.pool.acquire() as conn:
            row = await rb_repo.get(conn, rid)
        if not row:
            raise NotFound("Рассылка")
        return row

    async def cancel(self, rid: int) -> bool:
        async with self.pool.acquire() as conn:
            return await rb_repo.cancel_draft(conn, rid)

    async def start(self, rid: int, created_by: int) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await rb_repo.get(conn, rid)
                if not row or row["created_by"] != created_by:
                    raise NotFound("Рассылка")
                recipients = await rb_repo.recipients(conn, row["kind"], row["context_id"])
                claimed = await rb_repo.claim(conn, rid, len(recipients))
        if claimed is None:
            raise Conflict("ℹ️ Эта рассылка уже отправлена или отменена.")
        payload = Payload(row["content_type"], row["text"], row["file_id"])
        task = asyncio.create_task(self._run(rid, created_by, recipients, payload), name=f"result-broadcast-{rid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return len(recipients)

    async def retry_failed(self, rid: int, created_by: int) -> int:
        async with self.pool.acquire() as conn:
            row = await rb_repo.get(conn, rid)
            if not row or row["created_by"] != created_by:
                raise NotFound("Рассылка")
            if row["status"] != "done":
                raise Conflict("ℹ️ Повторить можно только уже завершённую рассылку.")
            recipients = await rb_repo.failed_recipients(conn, rid)
            if not recipients:
                raise Conflict("ℹ️ Нет получателей для повтора.")
            if not await rb_repo.start_retry(conn, rid):
                raise Conflict("ℹ️ Повтор уже выполняется.")
        payload = Payload(row["content_type"], row["text"], row["file_id"])
        task = asyncio.create_task(self._run(rid, created_by, recipients, payload, is_retry=True), name=f"result-retry-{rid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return len(recipients)

    async def _run(self, rid: int, admin_id: int, recipients: list[int], payload: Payload, *, is_retry: bool = False) -> None:
        sent = failed = blocked = 0

        async def on_result(uid: int, outcome: str) -> None:
            nonlocal sent, failed, blocked
            if outcome == "ok":
                sent += 1
                status = "sent"
            elif outcome == "blocked":
                blocked += 1
                status = "blocked"
            else:
                failed += 1
                status = "failed"
            async with self.pool.acquire() as conn:
                await rb_repo.record_delivery(conn, rid, uid, status)
                if status == "blocked":
                    await users_repo.mark_blocked(conn, uid)

        try:
            await deliver(self.bot, recipients, payload, interval=self.interval, on_result=on_result)
            status = "done"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            log.exception("result broadcast %s crashed", rid)
            status = "interrupted"
        finally:
            async with self.pool.acquire() as conn:
                if is_retry:
                    # merge into the totals already recorded on the first run
                    row = await rb_repo.get(conn, rid)
                    total_sent, total_failed, total_blocked = (row["sent"] or 0) + sent, failed, (row["blocked"] or 0) + blocked
                    await rb_repo.finish(conn, rid, status, total_sent, total_failed, total_blocked)
                else:
                    total_sent, total_failed, total_blocked = sent, failed, blocked
                    await rb_repo.finish(conn, rid, status, sent, failed, blocked)
        markup = None
        if total_failed:
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить неотправленным", callback_data=f"sn:retry:{rid}")]])
        try:
            await self.bot.send_message(
                admin_id,
                f"📨 Рассылка #{rid} {'завершена' if status == 'done' else 'прервана'}.\n"
                f"Доставлено: {total_sent}\nБот заблокирован: {total_blocked}\nОшибок: {total_failed}",
                reply_markup=markup,
            )
        except Exception:
            log.warning("could not notify %s about result broadcast %s", admin_id, rid)

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
