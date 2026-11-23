"""Flood limit: >20 updates in 10 s from one regular user are dropped with a single notice; staff exempt."""
from types import SimpleNamespace

import pytest

from app.middlewares.throttle import NOTICE, ThrottleMiddleware
from tests.harness import ADMIN_ID, Harness


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Roles:
    def __init__(self, deputies=()):
        self.deputies, self.calls = set(deputies), 0

    def is_main_admin(self, uid):
        return uid == 1

    async def is_staff(self, uid):
        self.calls += 1
        return uid == 1 or uid in self.deputies


async def _run(mw, uid, n):
    passed = 0

    async def handler(event, data):
        nonlocal passed
        passed += 1

    for _ in range(n):
        await mw(handler, object(), {"event_from_user": SimpleNamespace(id=uid)})
    return passed


async def test_limit_and_window_reset():
    clock = Clock()
    mw = ThrottleMiddleware(Roles(), limit=20, window=10, clock=clock)
    assert await _run(mw, 7, 25) == 20
    clock.now += 10
    assert await _run(mw, 7, 5) == 5  # the window has passed: works again


async def test_users_are_independent():
    mw = ThrottleMiddleware(Roles(), limit=3, window=10, clock=Clock())
    assert await _run(mw, 7, 5) == 3
    assert await _run(mw, 8, 3) == 3


async def test_staff_never_limited_and_cached():
    roles = Roles(deputies={5})
    mw = ThrottleMiddleware(roles, limit=3, window=10, clock=Clock())
    assert await _run(mw, 1, 50) == 50  # main admin: no DB lookup at all
    assert roles.calls == 0
    assert await _run(mw, 5, 50) == 50  # deputy
    assert roles.calls == 1  # looked up once, then cached


async def test_regular_user_below_limit_never_hits_db():
    roles = Roles()
    mw = ThrottleMiddleware(roles, limit=20, window=10, clock=Clock())
    assert await _run(mw, 7, 20) == 20
    assert roles.calls == 0


@pytest.mark.db
async def test_flood_through_bot_gives_one_notice(pool):
    h = Harness(pool)
    for _ in range(20):
        await h.say(7, "flooder01", "/start")
    h.clear()
    for _ in range(5):
        await h.say(7, "flooder01", "/start")
    assert h.texts().count(NOTICE) == 1
    assert len(h.texts()) == 1  # nothing else answered
    h.clear()
    for _ in range(3):
        await h.press(7, "flooder01", "me")
    assert not h.texts()  # button presses dropped silently (alert only once, already given)
    h.close()


@pytest.mark.db
async def test_admin_not_limited_through_bot(pool):
    h = Harness(pool)
    for _ in range(30):
        await h.say(ADMIN_ID, "boss", "/start")
    assert NOTICE not in h.texts()
    h.close()
