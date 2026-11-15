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
