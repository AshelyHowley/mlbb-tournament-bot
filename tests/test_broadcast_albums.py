"""General broadcast: documents and albums (2-10 photos/videos, or documents) are sent whole."""
import asyncio

import pytest
from aiogram.methods import SendDocument, SendMediaGroup

from app.handlers import admin_broadcast
from app.services.broadcast import MAX_ALBUM, BroadcastService, Payload, payload_from_row, validate_payload
from app.services.errors import ValidationError
from tests.harness import ADMIN_ID, Harness
from tests.test_broadcast_service import seed

pytestmark = pytest.mark.db


@pytest.fixture
def h(pool, monkeypatch):
    monkeypatch.setattr(admin_broadcast, "ALBUM_WAIT", 0.05)
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


async def _albums_done():
    while admin_broadcast._album_tasks:
        await asyncio.gather(*list(admin_broadcast._album_tasks))


def _groups(h, chat_id=None):
    return [m for m in h.tg.requests if isinstance(m, SendMediaGroup) and (chat_id is None or m.chat_id == chat_id)]


async def _open_composer(h, audience="all"):
    await h.press(ADMIN_ID, "boss", "adm:bc")
    await h.press(ADMIN_ID, "boss", f"bc:aud:{audience}")


async def test_album_is_collected_into_one_draft_and_sent_whole(h, pool):
    await seed(pool)
    await _open_composer(h)
    await h.media(ADMIN_ID, "boss", "photo", "P1", caption="Расписание", media_group_id="g1")
    await h.media(ADMIN_ID, "boss", "photo", "P2", media_group_id="g1")
    await h.media(ADMIN_ID, "boss", "video", "V3", media_group_id="g1")
    await _albums_done()
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT id, content_type, media FROM broadcasts")
    assert len(rows) == 1 and rows[0]["content_type"] == "album"  # one draft, not three
    assert any("Тип: <b>альбом (3 файлов)</b>" in t for t in h.texts())
    preview = _groups(h, ADMIN_ID)
    assert len(preview) == 1 and [m.media for m in preview[0].media] == ["P1", "P2", "V3"]
    assert preview[0].media[0].caption == "Расписание" and preview[0].media[1].caption is None

    bid = rows[0]["id"]
    h.clear()
    await h.press(ADMIN_ID, "boss", f"bc:send:{bid}")
    await asyncio.gather(*h.svc.bc._tasks)
    sent = _groups(h)
    assert sorted(m.chat_id for m in sent) == [1, 2, 10, 11, 300, 301]
    assert all([x.media for x in m.media] == ["P1", "P2", "V3"] for m in sent)
    assert (await h.svc.bc.get(bid))["status"] == "done"


async def test_document_broadcast(h, pool):
    await seed(pool)
    await _open_composer(h, "captains")
    await h.media(ADMIN_ID, "boss", "document", "D1", caption="Регламент")
    async with pool.acquire() as c:
        bid = await c.fetchval("SELECT id FROM broadcasts WHERE content_type='document'")
    assert bid and any("Тип: <b>документ</b>" in t for t in h.texts())
    h.clear()
    await h.press(ADMIN_ID, "boss", f"bc:send:{bid}")
    await asyncio.gather(*h.svc.bc._tasks)
    docs = [m for m in h.tg.requests if isinstance(m, SendDocument) and m.document == "D1"]
    assert sorted(m.chat_id for m in docs) == [1, 2]


async def test_document_album(h, pool):
    await seed(pool)
    await _open_composer(h)
    await h.media(ADMIN_ID, "boss", "document", "D1", media_group_id="g2")
    await h.media(ADMIN_ID, "boss", "document", "D2", caption="Файлы", media_group_id="g2")
    await _albums_done()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT content_type, text FROM broadcasts")
    assert (row["content_type"], row["text"]) == ("album", "Файлы")


async def test_album_cancelled_while_arriving_makes_no_draft(h, pool):
    await seed(pool)
    await _open_composer(h)
    await h.media(ADMIN_ID, "boss", "photo", "P1", media_group_id="g3")
    await h.press(ADMIN_ID, "boss", "bc:abort")
    await h.media(ADMIN_ID, "boss", "photo", "P2", media_group_id="g3")
    await _albums_done()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM broadcasts") == 0


async def test_too_big_album_rejected(h, pool):
    await seed(pool)
    await _open_composer(h)
    for i in range(MAX_ALBUM + 1):
        await h.media(ADMIN_ID, "boss", "photo", f"P{i}", media_group_id="g4")
    await _albums_done()
    assert "больше 10 файлов" in h.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM broadcasts") == 0


async def test_single_photo_and_text_unchanged(h, pool):
    await seed(pool)
    await _open_composer(h)
    await h.media(ADMIN_ID, "boss", "photo", "P1", caption="одно фото")
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT content_type, file_id, media FROM broadcasts")
    assert (row["content_type"], row["file_id"], row["media"]) == ("photo", "P1", None)


def test_album_validation():
    ok = Payload("album", "x", "a", (("photo", "a"), ("video", "b")))
    validate_payload(ok)
    for bad in (
        Payload("album", None, "a", (("photo", "a"),)),  # one item
        Payload("album", None, "a", tuple(("photo", str(i)) for i in range(11))),  # 11 items
        Payload("album", None, "a", (("photo", "a"), ("document", "b"))),  # documents mixed with photos
        Payload("album", None, "a", (("sticker", "a"), ("photo", "b"))),
        Payload("album", "x" * 1025, "a", (("photo", "a"), ("photo", "b"))),
    ):
        with pytest.raises(ValidationError):
            validate_payload(bad)


async def test_album_resume_and_pacing(pool):
    """Album survives the DB round trip (resume uses it) and is paced by its size."""
    await seed(pool)
    svc = BroadcastService(pool, None, interval=0.01)
    p = Payload("album", "cap", "a", (("photo", "a"), ("photo", "b"), ("video", "c")))
    bid, _ = await svc.prepare(42, "all", p)
    row = await svc.get(bid)
    assert payload_from_row(row) == p and p.weight == 3
