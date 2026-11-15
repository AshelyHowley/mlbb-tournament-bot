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
