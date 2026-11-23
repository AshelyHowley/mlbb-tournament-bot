"""Targeted, staff-composed notifications: a custom series win/loss message, or the tournament's
final placement broadcast. A small sibling of BroadcastService (same draft->sending atomic claim,
same retry-safe delivery loop) that never touches the general broadcast feature or its table."""
from __future__ import annotations

import asyncio
import logging

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
        self._spawn(rid, created_by, recipients, Payload(row["content_type"], row["text"], row["file_id"]))
        return len(recipients)

    async def retry_failed(self, rid: int, created_by: int) -> int:
        """done: resend to those whose delivery failed. interrupted: send to everyone in the audience
        it has not reached yet (never tried, or failed). Already delivered / blocked never get it twice."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await rb_repo.get(conn, rid)
                if not row or row["created_by"] != created_by:
                    raise NotFound("Рассылка")
                status = row["status"]
                if status not in ("done", "interrupted"):
                    raise Conflict("ℹ️ Повтор уже выполняется." if status == "sending"
                                   else "ℹ️ Эта рассылка ещё не отправлялась.")
                logged = await rb_repo.delivery_statuses(conn, rid)
                if status == "done":
                    recipients = [u for u, st in logged.items() if st == "failed"]
                else:
                    audience = await rb_repo.recipients(conn, row["kind"], row["context_id"])
                    recipients = sorted(
                        {u for u in audience if logged.get(u) not in ("sent", "blocked")}
                        | {u for u, st in logged.items() if st == "failed"}
                    )
                if not recipients and status == "done":
                    raise Conflict("ℹ️ Нет получателей для повтора.")
                if not await rb_repo.start_retry(conn, rid, status):
                    raise Conflict("ℹ️ Повтор уже выполняется.")
                if recipients:
                    await rb_repo.set_total(conn, rid, max(row["total"], len(set(logged) | set(recipients))))
                else:  # interrupted, but everyone was already reached: just close it
                    await self._finish_from_log(conn, rid, "done")
        if not recipients:
            raise Conflict("✅ Досылать некому: все получатели уже охвачены.")
        self._spawn(rid, created_by, recipients, Payload(row["content_type"], row["text"], row["file_id"]))
        return len(recipients)

    def _spawn(self, rid: int, admin_id: int, recipients: list[int], payload: Payload) -> None:
        task = asyncio.create_task(self._run(rid, admin_id, recipients, payload), name=f"result-broadcast-{rid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _finish_from_log(conn, rid: int, status: str) -> tuple[int, int, int]:
        """Closes the run with exact counters from the per-recipient log (never drift on interruption)."""
        c = await rb_repo.delivery_counts(conn, rid)
        sent, failed, blocked = c.get("sent", 0), c.get("failed", 0), c.get("blocked", 0)
        await rb_repo.finish(conn, rid, status, sent, failed, blocked)
        return sent, failed, blocked

    async def _run(self, rid: int, admin_id: int, recipients: list[int], payload: Payload) -> None:
        async def on_result(uid: int, outcome: str) -> None:
            status = {"ok": "sent", "blocked": "blocked"}.get(outcome, "failed")
            async with self.pool.acquire() as conn:
                await rb_repo.record_delivery(conn, rid, uid, status)
                if status == "blocked":
                    await users_repo.mark_blocked(conn, uid)

        async def close(status: str) -> tuple[int, int, int]:
            async with self.pool.acquire() as conn:
                return await self._finish_from_log(conn, rid, status)

        try:
            await deliver(self.bot, recipients, payload, interval=self.interval, on_result=on_result)
            status = "done"
        except asyncio.CancelledError:
            # shutdown: the author is told after the restart (notify_interrupted) and offered to resume
            await asyncio.shield(close("interrupted"))
            raise
        except Exception:
            log.exception("result broadcast %s crashed", rid)
            status = "interrupted"
        total_sent, total_failed, total_blocked = await close(status)
        if status == "interrupted":
            await self._offer_resume(rid, admin_id)
            return
        markup = None
        if total_failed:
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить неотправленным", callback_data=f"sn:retry:{rid}")]])
        try:
            await self.bot.send_message(
                admin_id,
                f"📨 Рассылка #{rid} завершена.\n"
                f"Доставлено: {total_sent}\nБот заблокирован: {total_blocked}\nОшибок: {total_failed}",
                reply_markup=markup,
            )
        except Exception:
            log.warning("could not notify %s about result broadcast %s", admin_id, rid)

    async def _offer_resume(self, rid: int, admin_id: int) -> None:
        async with self.pool.acquire() as conn:
            row = await rb_repo.get(conn, rid)
            c = await rb_repo.delivery_counts(conn, rid)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Дослать", callback_data=f"sn:retry:{rid}")]])
        try:
            await self.bot.send_message(
                admin_id,
                f"📨 Рассылка #{rid} прервана: доставлено {c.get('sent', 0)} из {row['total']}.\n"
                f"Бот заблокирован: {c.get('blocked', 0)}\nОшибок: {c.get('failed', 0)}\n\n"
                "Дослать тем, кому ещё не доставлено? Повторно сообщение никто не получит.",
                reply_markup=markup,
            )
        except Exception:
            log.warning("could not offer to resume result broadcast %s to %s", rid, admin_id)
            return
        async with self.pool.acquire() as conn:
            await rb_repo.mark_notified(conn, rid)

    async def notify_interrupted(self) -> int:
        """Called at startup (after recover): tells each author about notifications cut off by a restart."""
        async with self.pool.acquire() as conn:
            rows = await rb_repo.unnotified_interrupted(conn)
        for r in rows:
            await self._offer_resume(r["id"], r["created_by"])
        return len(rows)

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
