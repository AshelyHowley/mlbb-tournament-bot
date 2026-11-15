"""«💾 Скачать базу»: the main admin gets a consistent .db copy; nobody else does."""
import dataclasses
import sqlite3

import pytest
import pytest_asyncio
from aiogram.methods import SendDocument

from tests.harness import ADMIN_ID, Harness
from tests.helpers import make_team

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    hh.svc.cfg = dataclasses.replace(hh.cfg, database_url=f"sqlite+aiosqlite:///{pool.path}")
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


def _docs(h):
    return [m for m in h.tg.requests if isinstance(m, SendDocument)]


async def test_admin_downloads_a_working_copy(h, pool, tmp_path):
    await make_team(pool, 1, "Alpha")
    await h.press(ADMIN_ID, "adm", "adm:backup")
    main = next(d for d in _docs(h) if d.document.filename.startswith("tournament_"))
    assert main.chat_id == ADMIN_ID and main.document.filename.endswith(".db")
    f = tmp_path / "copy.db"
    f.write_bytes(main.document.data)
    c = sqlite3.connect(f)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT name FROM teams").fetchall() == [("Alpha",)]
    assert "отправлена" in h.last()


async def test_backup_button_only_for_main_admin(h, pool):
    await h.press(ADMIN_ID, "adm", "adm")
    assert "💾 Скачать базу" in h.buttons()
    h.clear()
    await h.press(5, "someone1", "adm:backup")  # not staff at all
    assert _docs(h) == []


# ------------------------------------------------------------------ automatic backups
from app.services.db_backup import AutoBackup  # noqa: E402


def _auto(h, pool):
    return AutoBackup(h.bot, h.svc.cfg, h.svc.archive.path, every_hours=3)


async def test_auto_backup_skips_empty_database(h, pool):
    assert await _auto(h, pool).run_once() is False
    assert _docs(h) == []


async def test_auto_backup_only_when_changed(h, pool):
    auto = _auto(h, pool)
    await make_team(pool, 1, "Alpha")
    assert await auto.run_once() is True
    main = [d for d in _docs(h) if d.document.filename.startswith("tournament_")]
    assert len(main) == 1 and main[0].chat_id == ADMIN_ID and main[0].disable_notification is True
    assert await auto.run_once() is False  # nothing changed -> no new copy
    await make_team(pool, 2, "Bravo")
    assert await auto.run_once() is True


async def test_auto_backup_on_shutdown(h, pool):
    auto = _auto(h, pool)
    await make_team(pool, 1, "Alpha")
    await auto.shutdown()
    assert any("перед остановкой" in (d.caption or "") for d in _docs(h))


async def test_auto_backup_can_be_turned_off(h, pool):
    await make_team(pool, 1, "Alpha")
    auto = AutoBackup(h.bot, h.svc.cfg, h.svc.archive.path, every_hours=0)
    auto.start()
    await auto.shutdown()
    assert _docs(h) == []


def test_backup_hours_setting(tmp_path):
    from app.config import ConfigError, load_config
    env = {"BOT_TOKEN": "123456:" + "A" * 35, "ADMIN_TELEGRAM_ID": "42", "DATABASE_URL": "sqlite:///x.db"}
    assert load_config(tmp_path / "none.env", env).backup_every_hours == 3
    assert load_config(tmp_path / "none.env", {**env, "BACKUP_EVERY_HOURS": "0"}).backup_every_hours == 0
    with pytest.raises(ConfigError):
        load_config(tmp_path / "none.env", {**env, "BACKUP_EVERY_HOURS": "soon"})


# ------------------------------------------------------------------ restore from a backup file
from app.handlers import admin_panel  # noqa: E402


async def _backup_bytes(h, pool) -> bytes:
    await h.press(ADMIN_ID, "adm", "adm:backup")
    doc = next(d for d in _docs(h) if d.document.filename.startswith("tournament_"))
    return doc.document.data


@pytest.fixture
def uploaded(monkeypatch):
    """What the admin 'sends' to the bot: tests set .data, _download returns it instead of Telegram."""
    box = type("Box", (), {"data": b""})()

    async def fake_download(bot, document):
        return box.data

    monkeypatch.setattr(admin_panel, "_download", fake_download)
    return box


async def test_restore_brings_back_deleted_teams(h, pool, uploaded):
    await make_team(pool, 1, "Alpha")
    await make_team(pool, 2, "Bravo")
    uploaded.data = await _backup_bytes(h, pool)
    async with pool.acquire() as c:                       # the "wipe"
        await c.execute("DELETE FROM teams")
    h.clear()
    await h.press(ADMIN_ID, "adm", "adm:restore")
    await h.media(ADMIN_ID, "adm", "document", "f1")
    assert "команд: <b>2</b>" in h.last()
    await h.press(ADMIN_ID, "adm", "adm:restore:ok")
    assert "База восстановлена" in h.last()
    assert any("До восстановления" in (d.caption or "") for d in _docs(h))  # safety copy first
    async with pool.acquire() as c:
        assert sorted(r["name"] for r in await c.fetch("SELECT name FROM teams")) == ["Alpha", "Bravo"]


async def test_restore_rejects_garbage_and_keeps_data(h, pool, uploaded):
    await make_team(pool, 1, "Alpha")
    uploaded.data = b"definitely not sqlite" * 100
    await h.press(ADMIN_ID, "adm", "adm:restore")
    await h.media(ADMIN_ID, "adm", "document", "f1")
    assert "не файл базы" in h.last()
    await h.press(ADMIN_ID, "adm", "adm:restore:ok")      # no confirm state -> nothing happens
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == 1


async def test_restore_cancel_changes_nothing(h, pool, uploaded):
    await make_team(pool, 1, "Alpha")
    uploaded.data = await _backup_bytes(h, pool)
    await make_team(pool, 2, "Bravo")
    await h.press(ADMIN_ID, "adm", "adm:restore")
    await h.media(ADMIN_ID, "adm", "document", "f1")
    await h.press(ADMIN_ID, "adm", "adm:restore:no")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == 2


async def test_restore_player_archive_file(h, pool, uploaded, tmp_path):
    import sqlite3 as s3
    f = tmp_path / "arch.db"
    c = s3.connect(f)
    c.execute("CREATE TABLE players (user_id INTEGER PRIMARY KEY, stars INTEGER)")
    c.executemany("INSERT INTO players VALUES (?, ?)", [(1, 10), (2, 20)])
    c.commit(); c.close()
    uploaded.data = f.read_bytes()
    await h.press(ADMIN_ID, "adm", "adm:restore")
    await h.media(ADMIN_ID, "adm", "document", "f1")
    assert "Архив игроков" in h.last() and "<b>2</b>" in h.last()
    await h.press(ADMIN_ID, "adm", "adm:restore:ok")
    assert "игроков в архиве: <b>2</b>" in h.last()


async def test_restore_only_for_main_admin(h, pool, uploaded):
    await h.press(5, "someone1", "adm:restore")
    await h.media(5, "someone1", "document", "f1")
    assert not any("Проверьте файл" in t for t in h.texts())
