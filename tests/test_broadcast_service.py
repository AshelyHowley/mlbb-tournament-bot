import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError

from app.repositories import broadcasts as bc_repo
from app.services.broadcast import BroadcastService, Payload
from app.services.errors import Conflict, NotFound, ValidationError
from app.services.registration import RegistrationService, TgUser
from app.services.team import TeamService
from tests.helpers import add_user, dummy_method

pytestmark = pytest.mark.db


class Bot:
    def __init__(self, forbidden=()):
        self.forbidden, self.sent = set(forbidden), []

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(dummy_method(), "bot was blocked by the user")
        self.sent.append((chat_id, text))

    send_photo = send_video = send_message


async def seed(pool):
    """2 teams (captains 1 and 2; team 1 has two more members), 2 solo players, 1 user who never registered."""
    teams, reg = TeamService(pool), RegistrationService(pool)
    v = await teams.create_team(TgUser(1, "captain1", "C1"), "Alpha")
    await teams.join_team(TgUser(300, None, "Mate"), v.invite_token)
    await teams.join_team(TgUser(301, "member01", "Mate2"), v.invite_token)
    await teams.create_team(TgUser(2, "captain2", "C2"), "Beta")
    await reg.register_solo(TgUser(10, "solo0001", "S1"), "EXP")
    await reg.register_solo(TgUser(11, None, "S2"), "MID")
    await add_user(pool, 999, "nobody01")  # started the bot, not registered

async def recipients(pool, aud):
    async with pool.acquire() as c:
        return await bc_repo.recipients(c, aud)


async def test_audiences(pool):
    await seed(pool)
    assert await recipients(pool, "captains") == [1, 2]
    assert await recipients(pool, "solo") == [10, 11]
    assert await recipients(pool, "all") == [1, 2, 10, 11, 300, 301]  # every team member and solo player; not 999


async def test_registrar_reached_while_team_has_no_captain(pool):
    await seed(pool)
    teams = TeamService(pool)
    v = await teams.create_team(TgUser(50, "registr1", "R"), "Gamma", role="registrar")
    assert await recipients(pool, "captains") == [1, 2, 50]
    assert 50 in await recipients(pool, "all")
    await teams.accept_captain(TgUser(60, "newcapt1", "NC"), v.captain_invite_token)
    assert await recipients(pool, "captains") == [1, 2, 60]  # the captain replaces the registrar
    assert {50, 60} <= set(await recipients(pool, "all"))  # the registrar still gets general news


async def test_sender_never_receives_own_broadcast(pool):
    await seed(pool)
    assert await recipients(pool, "captains") == [1, 2]
    async with pool.acquire() as c:
        assert await bc_repo.recipients(c, "captains", exclude=1) == [2]


async def test_solo_audience_skips_players_assembled_into_teams(pool):
    from app.services import solo_teams
    await seed(pool)
    reg = RegistrationService(pool)
    for uid in (12, 13, 14):
        await reg.register_solo(TgUser(uid, f"solo{uid:04d}", f"S{uid}"), "EXP")
    for uid in (15, 16):  # 7 solos -> one team of 5, two left over
        await reg.register_solo(TgUser(uid, f"solo{uid:04d}", f"S{uid}"), "MID")
    plan, _ = await solo_teams.assemble(pool)
    built = {s.player.user_id for s in plan.teams[0].seats}
    left = {p.user_id for p in plan.leftovers}
    assert await recipients(pool, "solo") == sorted(left)
    everyone = set(await recipients(pool, "all"))
    assert built | left <= everyone  # assembled players are still reached as team players
    assert plan.teams[0].captain.user_id in await recipients(pool, "captains")


async def test_blocked_users_excluded(pool):
    await seed(pool)
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET is_blocked = TRUE WHERE telegram_id = 2")
    assert await recipients(pool, "captains") == [1]


async def test_send_flow_and_double_click(pool):
    await seed(pool)
    bot = Bot(forbidden={11})
    svc = BroadcastService(pool, bot, interval=0)
    bid, count = await svc.prepare(42, "solo", Payload("text", "Hello"))
    assert count == 2
    results = await asyncio.gather(*[svc.start(bid, 42) for _ in range(10)], return_exceptions=True)
    assert sum(isinstance(r, int) for r in results) == 1
    assert all(isinstance(r, Conflict) for r in results if not isinstance(r, int))
    await asyncio.gather(*svc._tasks)
    row = await svc.get(bid)
    assert (row["status"], row["sent"], row["blocked"], row["failed"]) == ("done", 1, 1, 0)
    assert [c for c, _ in bot.sent if c != 42] == [10]  # exactly one delivery to the reachable solo player
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT is_blocked FROM users WHERE telegram_id=11") == True
    assert await recipients(pool, "solo") == [10]


async def test_only_owner_can_start_and_draft_can_be_cancelled(pool):
    await seed(pool)
    svc = BroadcastService(pool, Bot(), interval=0)
    bid, _ = await svc.prepare(42, "all", Payload("photo", "cap", "FILE"))
    with pytest.raises(NotFound):
        await svc.start(bid, 43)
    assert await svc.cancel(bid)
    assert not await svc.cancel(bid)
    with pytest.raises(Conflict):
        await svc.start(bid, 42)
    with pytest.raises(NotFound):
        await svc.start(99999, 42)


async def test_prepare_validates(pool):
    svc = BroadcastService(pool, Bot())
    with pytest.raises(ValidationError):
        await svc.prepare(1, "nobody", Payload("text", "x"))
    with pytest.raises(ValidationError):
        await svc.prepare(1, "all", Payload("text", "   "))


async def test_recover_marks_stale(pool):
    svc = BroadcastService(pool, Bot())
    bid, _ = await svc.prepare(42, "all", Payload("text", "x"))
    async with pool.acquire() as c:
        await c.execute("UPDATE broadcasts SET status='sending' WHERE id=$1", bid)
    assert await svc.recover() == 1
    assert (await svc.get(bid))["status"] == "interrupted"


async def test_shutdown_cancels_running_broadcast_and_marks_interrupted(pool):
    await seed(pool)
    svc = BroadcastService(pool, Bot(), interval=0.2)
    bid, _ = await svc.prepare(42, "all", Payload("text", "x"))
    await svc.start(bid, 42)
    await asyncio.sleep(0.05)
    await svc.shutdown()
    assert (await svc.get(bid))["status"] == "interrupted"


# ------------------------------------------------------------------ resume after interruption
class RecBot(Bot):
    """Also records reply_markup so the resume offer can be checked."""

    def __init__(self, forbidden=()):
        super().__init__(forbidden)
        self.markups = []

    async def send_message(self, chat_id, text, **kw):
        await super().send_message(chat_id, text, **kw)
        self.markups.append((chat_id, kw.get("reply_markup")))

    send_photo = send_video = send_message


async def _interrupt_after(pool, svc, bid, n_done):
    """Simulates a broadcast cut off after the first n recipients were sent to."""
    async with pool.acquire() as c:
        row = await bc_repo.get(c, bid)
        rec = await bc_repo.recipients(c, row["audience"], exclude=row["admin_id"])
        await bc_repo.claim(c, bid, len(rec))
        for uid in rec[:n_done]:
            await bc_repo.record_delivery(c, bid, uid, "ok")
    assert await svc.recover() == 1
    return rec


async def test_restart_offers_resume_and_resume_skips_already_sent(pool):
    await seed(pool)
    bot = RecBot()
    svc = BroadcastService(pool, bot, interval=0)
    bid, _ = await svc.prepare(42, "all", Payload("text", "hello"))
    rec = await _interrupt_after(pool, svc, bid, 3)

    assert await svc.notify_interrupted() == 1
    offer = [t for uid, t in bot.sent if uid == 42][-1]
    assert f"#{bid} прервана" in offer and "доставлено 3 из" in offer
    markup = [m for uid, m in bot.markups if uid == 42][-1]
    assert markup.inline_keyboard[0][0].callback_data == f"bc:resume:{bid}"
    assert await svc.notify_interrupted() == 0  # told once

    bot.sent.clear()
    left = await svc.resume(bid, 42)
    assert left == len(rec) - 3
    await asyncio.gather(*svc._tasks)
    got = [uid for uid, t in bot.sent if t == "hello"]
    assert sorted(got) == sorted(rec[3:])  # nobody gets it twice
    row = await svc.get(bid)
    assert (row["status"], row["sent"], row["total"]) == ("done", len(rec), len(rec))
    assert "завершена" in [t for uid, t in bot.sent if uid == 42][-1]


async def test_resume_double_click_and_owner_only(pool):
    await seed(pool)
    svc = BroadcastService(pool, RecBot(), interval=0.2)
    bid, _ = await svc.prepare(42, "all", Payload("text", "hello"))
    await _interrupt_after(pool, svc, bid, 1)
    with pytest.raises(NotFound):
        await svc.resume(bid, 43)
    await svc.resume(bid, 42)
    with pytest.raises(Conflict):
        await svc.resume(bid, 42)
    await svc.shutdown()


async def test_resume_includes_newcomers(pool):
    await seed(pool)
    bot = RecBot()
    svc = BroadcastService(pool, bot, interval=0)
    bid, _ = await svc.prepare(42, "solo", Payload("text", "hello"))
    await _interrupt_after(pool, svc, bid, 2)  # both solo players already reached
    await RegistrationService(pool).register_solo(TgUser(12, "solo0003", "S3"), "GOLD")
    assert await svc.resume(bid, 42) == 1
    await asyncio.gather(*svc._tasks)
    assert [uid for uid, t in bot.sent if t == "hello"] == [12]


async def test_resume_with_nobody_left_closes_as_done(pool):
    await seed(pool)
    svc = BroadcastService(pool, RecBot(), interval=0)
    bid, _ = await svc.prepare(42, "solo", Payload("text", "hello"))
    await _interrupt_after(pool, svc, bid, 2)
    assert await svc.resume(bid, 42) == 0
    assert (await svc.get(bid))["status"] == "done"


async def test_old_untracked_broadcast_cannot_be_resumed(pool):
    await seed(pool)
    svc = BroadcastService(pool, RecBot(), interval=0)
    bid, _ = await svc.prepare(42, "all", Payload("text", "hello"))
    async with pool.acquire() as c:
        await c.execute("UPDATE broadcasts SET status='interrupted', tracked=0 WHERE id=$1", bid)
    assert await svc.notify_interrupted() == 0
    with pytest.raises(Conflict):
        await svc.resume(bid, 42)


async def test_shutdown_keeps_exact_counts_then_resume_finishes(pool):
    await seed(pool)
    bot = RecBot()
    svc = BroadcastService(pool, bot, interval=0.05)
    bid, total = await svc.prepare(42, "all", Payload("text", "hello"))
    await svc.start(bid, 42)
    await asyncio.sleep(0.12)
    await svc.shutdown()
    first = [uid for uid, t in bot.sent if t == "hello"]
    row = await svc.get(bid)
    assert row["status"] == "interrupted" and row["sent"] == len(first) and 0 < len(first) < total

    svc2 = BroadcastService(pool, bot, interval=0)  # "after restart"
    assert await svc2.notify_interrupted() == 1
    await svc2.resume(bid, 42)
    await asyncio.gather(*svc2._tasks)
    got = [uid for uid, t in bot.sent if t == "hello"]
    assert len(got) == len(set(got)) == total
    assert (await svc2.get(bid))["status"] == "done"


async def test_resume_buttons_through_bot(pool):
    from tests.harness import ADMIN_ID, Harness
    await seed(pool)
    h = Harness(pool)
    bid, _ = await h.svc.bc.prepare(ADMIN_ID, "all", Payload("text", "hello"))
    rec = await _interrupt_after(pool, h.svc.bc, bid, 2)
    await h.press(7, "stranger1", f"bc:resume:{bid}")  # not staff: ignored
    assert (await h.svc.bc.get(bid))["status"] == "interrupted"
    await h.press(ADMIN_ID, "boss", f"bc:noresume:{bid}")
    assert "не будет дослана" in h.last()
    await h.press(ADMIN_ID, "boss", f"bc:resume:{bid}")
    assert f"Осталось получателей: <b>{len(rec) - 2}</b>" in h.last()
    await asyncio.gather(*h.svc.bc._tasks)
    assert (await h.svc.bc.get(bid))["status"] == "done"
    h.clear()
    await h.press(ADMIN_ID, "boss", f"bc:resume:{bid}")
    assert "уже досылается или завершена" in h.last()
    assert not h.errors
    h.close()
