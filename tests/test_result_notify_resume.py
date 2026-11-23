"""Result notifications: an interrupted run is reported after a restart and can be resumed without duplicates."""
import asyncio

import pytest

from app.repositories import result_broadcasts as rb_repo
from app.services.broadcast import Payload
from app.services.errors import Conflict, NotFound
from app.services.result_notify import ResultNotifyService
from tests.helpers import add_user
from tests.test_broadcast_service import RecBot, seed

pytestmark = pytest.mark.db
ADMIN = 42


async def _draft(pool, svc):
    await seed(pool)
    await add_user(pool, ADMIN, "organizer")
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM tournaments WHERE status='active'")
    rid, count = await svc.prepare(ADMIN, "final_all", tid, Payload("text", "итоги"))
    return rid, count


async def _interrupt_after(pool, svc, rid, outcomes):
    """Claims the draft and logs the given {uid: status} as if the run was cut off right after them."""
    async with pool.acquire() as c:
        row = await rb_repo.get(c, rid)
        rec = await rb_repo.recipients(c, row["kind"], row["context_id"])
        await rb_repo.claim(c, rid, len(rec))
        for uid, st in outcomes.items():
            await rb_repo.record_delivery(c, rid, uid, st)
    assert await svc.recover() == 1
    return rec


def _got(bot):
    return [uid for uid, t in bot.sent if t == "итоги"]


async def test_restart_offer_then_resume_skips_sent_and_blocked(pool):
    bot = RecBot()
    svc = ResultNotifyService(pool, bot, interval=0)
    rid, _ = await _draft(pool, svc)
    async with pool.acquire() as c:
        rec = await rb_repo.recipients(c, "final_all", (await rb_repo.get(c, rid))["context_id"])
    sent, blocked, failed = rec[0], rec[1], rec[2]
    await _interrupt_after(pool, svc, rid, {sent: "sent", blocked: "blocked", failed: "failed"})

    assert await svc.notify_interrupted() == 1
    offer = [t for uid, t in bot.sent if uid == ADMIN][-1]
    assert f"#{rid} прервана" in offer and f"доставлено 1 из {len(rec)}" in offer
    assert [m for uid, m in bot.markups if uid == ADMIN][-1].inline_keyboard[0][0].callback_data == f"sn:retry:{rid}"
    assert await svc.notify_interrupted() == 0  # told once

    left = await svc.retry_failed(rid, ADMIN)
    assert left == len(rec) - 2  # everyone except the delivered and the blocked one; the failed one is retried
    await asyncio.gather(*svc._tasks)
    assert sorted(_got(bot)) == sorted(u for u in rec if u not in (sent, blocked))
    row = await svc.get(rid)
    assert (row["status"], row["sent"], row["blocked"], row["failed"], row["total"]) == ("done", len(rec) - 1, 1, 0, len(rec))


async def test_resume_is_owner_only_and_double_click_safe(pool):
    svc = ResultNotifyService(pool, RecBot(), interval=0.2)
    rid, _ = await _draft(pool, svc)
    await _interrupt_after(pool, svc, rid, {})
    with pytest.raises(NotFound):
        await svc.retry_failed(rid, 43)
    await svc.retry_failed(rid, ADMIN)
    with pytest.raises(Conflict):
        await svc.retry_failed(rid, ADMIN)
    await svc.shutdown()


async def test_interrupted_with_everyone_reached_closes_as_done(pool):
    svc = ResultNotifyService(pool, RecBot(), interval=0)
    rid, _ = await _draft(pool, svc)
    async with pool.acquire() as c:
        rec = await rb_repo.recipients(c, "final_all", (await rb_repo.get(c, rid))["context_id"])
    await _interrupt_after(pool, svc, rid, {u: "sent" for u in rec})
    with pytest.raises(Conflict, match="Досылать некому"):
        await svc.retry_failed(rid, ADMIN)
    row = await svc.get(rid)
    assert (row["status"], row["sent"]) == ("done", len(rec))


async def test_done_retry_only_failed(pool):
    bot = RecBot()
    svc = ResultNotifyService(pool, bot, interval=0)
    rid, _ = await _draft(pool, svc)
    async with pool.acquire() as c:
        rec = await rb_repo.recipients(c, "final_all", (await rb_repo.get(c, rid))["context_id"])
        await rb_repo.claim(c, rid, len(rec))
        for u in rec:
            await rb_repo.record_delivery(c, rid, u, "failed" if u == rec[0] else "sent")
        await rb_repo.finish(c, rid, "done", len(rec) - 1, 1, 0)
    assert await svc.retry_failed(rid, ADMIN) == 1
    await asyncio.gather(*svc._tasks)
    assert _got(bot) == [rec[0]]
    row = await svc.get(rid)
    assert (row["status"], row["sent"], row["failed"]) == ("done", len(rec), 0)


async def test_shutdown_mid_run_keeps_exact_counts_and_resume_completes(pool):
    bot = RecBot()
    svc = ResultNotifyService(pool, bot, interval=0.05)
    rid, total = await _draft(pool, svc)
    await svc.start(rid, ADMIN)
    await asyncio.sleep(0.12)
    await svc.shutdown()
    first = _got(bot)
    row = await svc.get(rid)
    assert row["status"] == "interrupted" and row["sent"] == len(first) and 0 < len(first) < total

    svc2 = ResultNotifyService(pool, bot, interval=0)
    assert await svc2.notify_interrupted() == 1
    await svc2.retry_failed(rid, ADMIN)
    await asyncio.gather(*svc2._tasks)
    got = _got(bot)
    assert len(got) == len(set(got)) == total
    assert (await svc2.get(rid))["status"] == "done"


async def test_old_interrupted_ones_are_not_announced(pool):
    """Migration 025 marks notifications interrupted before the update as already told."""
    svc = ResultNotifyService(pool, RecBot(), interval=0)
    rid, _ = await _draft(pool, svc)
    async with pool.acquire() as c:
        await c.execute("UPDATE result_broadcasts SET status='interrupted', notified=1 WHERE id=$1", rid)
    assert await svc.notify_interrupted() == 0
