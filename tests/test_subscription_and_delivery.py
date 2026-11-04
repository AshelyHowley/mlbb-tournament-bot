import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from app.services.broadcast import MAX_ATTEMPTS, Payload, deliver, validate_payload
from app.services.errors import ValidationError
from app.services.subscription import SubscriptionService, is_member_status
from tests.helpers import dummy_method


# ----------------------------------------------------------------- subscription
class ChatBot:
    def __init__(self, table):
        self.table, self.calls = table, []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        v = self.table[chat_id]
        if isinstance(v, BaseException):
            raise v
        return SimpleNamespace(status=v[0], is_member=v[1] if len(v) > 1 else None)


@pytest.mark.parametrize("status,ok", [("member", True), ("administrator", True), ("creator", True),
                                       ("left", False), ("kicked", False), ("restricted", False)])
def test_status_mapping(status, ok):
    assert is_member_status(SimpleNamespace(status=status, is_member=False)) is ok


def test_restricted_but_member_counts():
    assert is_member_status(SimpleNamespace(status="restricted", is_member=True))


async def test_subscribed_both():
    svc = SubscriptionService(ChatBot({"@c": ("member",), "@t": ("creator",)}), "@c", "@t")
    assert (await svc.check(1)).ok


@pytest.mark.parametrize("ch,chat,expected", [("left", "member", (False, True)), ("member", "kicked", (True, False))])
async def test_not_subscribed(ch, chat, expected):
    svc = SubscriptionService(ChatBot({"@c": (ch,), "@t": (chat,)}), "@c", "@t")
    r = await svc.check(1)
    assert not r.ok and (r.channel_ok, r.chat_ok) == expected and not r.error


async def test_api_error_never_allows():
    err = TelegramBadRequest(dummy_method(), "chat not found")
    svc = SubscriptionService(ChatBot({"@c": ("member",), "@t": err}), "@c", "@t")
    r = await svc.check(1)
    assert r.error and not r.ok


async def test_timeout_never_allows(monkeypatch):
    import app.services.subscription as sub

    monkeypatch.setattr(sub, "_TIMEOUT", 0.05)

    class Slow:
        async def get_chat_member(self, *a):
            await asyncio.sleep(1)

    r = await SubscriptionService(Slow(), "@c", "@t").check(1)
    assert r.error and not r.ok


# ----------------------------------------------------------------- broadcast delivery
class SendBot:
    def __init__(self, script=None):
        self.script = script or {}  # uid -> list of exceptions (consumed in order)
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        assert kw.get("parse_mode") is None  # admin text is never parsed as HTML
        q = self.script.get(chat_id)
        if q:
            e = q.pop(0)
            if e:
                raise e
        self.sent.append(chat_id)

    async def send_photo(self, chat_id, file_id, **kw):
        assert kw["parse_mode"] is None
        self.sent.append(("photo", chat_id, file_id, kw["caption"]))

    async def send_video(self, chat_id, file_id, **kw):
        self.sent.append(("video", chat_id, file_id, kw["caption"]))


async def nosleep(_):
    return None


async def test_one_failure_does_not_stop_others():
    bad = TelegramBadRequest(dummy_method(), "chat not found")
    bot = SendBot({2: [bad], 3: [TelegramForbiddenError(dummy_method(), "blocked")]})
    blocked = []

    async def on_blocked(uid):
        blocked.append(uid)

    st = await deliver(bot, [1, 2, 3, 4], Payload("text", "hi"), sleep=nosleep, on_blocked=on_blocked)
    assert (st.sent, st.failed, st.blocked) == (2, 1, 1)
    assert bot.sent == [1, 4] and blocked == [3]


async def test_retry_after_is_honoured_then_succeeds():
    slept = []

    async def sleep(s):
        slept.append(s)

    bot = SendBot({1: [TelegramRetryAfter(dummy_method(), "flood", 3)]})
    st = await deliver(bot, [1, 2], Payload("text", "hi"), sleep=sleep, interval=0)
    assert st.sent == 2 and slept and slept[0] >= 3


async def test_retry_after_gives_up_eventually():
    bot = SendBot({1: [TelegramRetryAfter(dummy_method(), "flood", 1)] * (MAX_ATTEMPTS + 2)})
    st = await deliver(bot, [1, 2], Payload("text", "x"), sleep=nosleep)
    assert (st.sent, st.failed) == (1, 1)


async def test_network_error_retried():
    bot = SendBot({1: [TelegramNetworkError(dummy_method(), "boom")]})
    st = await deliver(bot, [1], Payload("text", "x"), sleep=nosleep)
    assert st.sent == 1


async def test_unexpected_exception_isolated():
    bot = SendBot({1: [RuntimeError("bug")]})
    st = await deliver(bot, [1, 2], Payload("text", "x"), sleep=nosleep)
    assert (st.sent, st.failed) == (1, 1)


async def test_media_uses_file_id():
    bot = SendBot()
    await deliver(bot, [5], Payload("photo", "cap", "FILEID"), sleep=nosleep)
    await deliver(bot, [5], Payload("video", None, "VID"), sleep=nosleep)
    assert bot.sent == [("photo", 5, "FILEID", "cap"), ("video", 5, "VID", None)]


async def test_delivery_does_not_block_event_loop():
    """500 recipients with pacing must yield to the loop (a ticker keeps running)."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    t = asyncio.create_task(ticker())
    await deliver(SendBot(), list(range(500)), Payload("text", "x"), interval=0.0005)
    t.cancel()
    assert ticks > 3


def test_payload_validation():
    validate_payload(Payload("text", "ok"))
    for bad in (Payload("text", "  "), Payload("text", "x" * 4097), Payload("photo", None, None),
                Payload("photo", "c" * 1025, "f"), Payload("audio", "x", "f")):
        with pytest.raises(ValidationError):
            validate_payload(bad)
