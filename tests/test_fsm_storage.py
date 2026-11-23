"""Dialog state persists across restarts (fsm.db), expires after the TTL, and never touches tournament.db."""
import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from app.services.fsm_storage import SQLiteStorage, fsm_path_for
from tests.harness import Harness
from tests.test_handlers import counts

KEY = StorageKey(bot_id=1, chat_id=7, user_id=7)


class Form(StatesGroup):
    step = State()


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


async def test_roundtrip_and_survives_reopen(tmp_path):
    path = str(tmp_path / "fsm.db")
    s = SQLiteStorage(path)
    await s.set_state(KEY, Form.step)
    await s.update_data(KEY, {"name": "Альфа", "roles": ["MID", "EXP"], "stars": 100})
    await s.close()

    s2 = SQLiteStorage(path)
    assert await s2.get_state(KEY) == "Form:step"
    assert await s2.get_data(KEY) == {"name": "Альфа", "roles": ["MID", "EXP"], "stars": 100}
    assert await s2.get_value(KEY, "stars") == 100
    assert await s2.get_state(StorageKey(bot_id=1, chat_id=8, user_id=8)) is None
    await s2.close()


async def test_clear_removes_row(tmp_path):
    s = SQLiteStorage(str(tmp_path / "fsm.db"))
    await s.set_state(KEY, Form.step)
    await s.set_data(KEY, {"a": 1})
    await s.set_state(KEY, None)
    await s.set_data(KEY, {})
    db = await s._conn()
    async with db.execute("SELECT COUNT(*) FROM fsm") as cur:
        assert (await cur.fetchone())[0] == 0
    await s.close()


async def test_data_without_state_is_kept(tmp_path):
    s = SQLiteStorage(str(tmp_path / "fsm.db"))
    await s.set_data(KEY, {"x": 1})
    assert await s.get_state(KEY) is None and await s.get_data(KEY) == {"x": 1}
    await s.close()


async def test_ttl_expires_untouched_state(tmp_path):
    clock = Clock()
    s = SQLiteStorage(str(tmp_path / "fsm.db"), ttl=24 * 3600, clock=clock)
    await s.set_state(KEY, Form.step)
    await s.update_data(KEY, {"a": 1})
    clock.now += 23 * 3600
    assert await s.get_state(KEY) == "Form:step"
    await s.update_data(KEY, {"b": 2})  # any write refreshes the timer
    clock.now += 23 * 3600
    assert await s.get_data(KEY) == {"a": 1, "b": 2}
    clock.now += 25 * 3600
    assert await s.get_state(KEY) is None and await s.get_data(KEY) == {}
    await s.close()


async def test_expired_rows_purged_on_open(tmp_path):
    path, clock = str(tmp_path / "fsm.db"), Clock()
    s = SQLiteStorage(path, ttl=3600, clock=clock)
    await s.set_state(KEY, Form.step)
    await s.close()
    clock.now += 7200
    s2 = SQLiteStorage(path, ttl=3600, clock=clock)
    db = await s2._conn()
    async with db.execute("SELECT COUNT(*) FROM fsm") as cur:
        assert (await cur.fetchone())[0] == 0
    await s2.close()


@pytest.mark.parametrize("url, expected", [
    ("sqlite:///C:/data/tournament.db", "fsm.db"),
    ("/data/tournament.db", "fsm.db"),
])
def test_path_next_to_main_db(url, expected, tmp_path):
    from pathlib import Path
    from app.services.player_archive import archive_path_for
    p = Path(fsm_path_for(url))
    assert p.name == expected and p.parent == Path(archive_path_for(url)).parent


@pytest.mark.db
async def test_registration_continues_after_restart(pool, tmp_path):
    path = str(tmp_path / "fsm.db")
    storage = SQLiteStorage(path)
    h = Harness(pool, storage=storage)
    await h.press(7, "sologuy01", "reg:start")
    await h.press(7, "sologuy01", "reg:solo")
    await h.pick_solo_role(7, "sologuy01", "JUNGLE")
    assert "JUNGLE" in h.last()
    await storage.close()
    h.close()

    # "restart": brand new dispatcher + storage over the same file
    storage2 = SQLiteStorage(path)
    h2 = Harness(pool, storage=storage2)
    await h2.press(7, "sologuy01", "reg:confirm")
    assert await counts(pool) == (0, 1, 0)
    assert not h2.errors, [r.getMessage() for r in h2.errors]
    await storage2.close()
    h2.close()
