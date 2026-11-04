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
        assert await c.fetchval("SELECT is_blocked FROM users WHERE telegram_id=11") is True
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
