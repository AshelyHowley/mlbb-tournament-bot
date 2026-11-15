"""The player archive lives in its own file and survives a wiped main database."""
from __future__ import annotations

import asyncio

from app.services.player_archive import PlayerArchive, archive_path_for


def test_archive_path_is_next_to_main_db(tmp_path):
    p = archive_path_for(f"sqlite+aiosqlite:///{tmp_path / 'tournament.db'}")
    assert p.endswith("player_archive.db")
    assert str(tmp_path) in p


def test_save_get_and_update(tmp_path):
    async def run():
        a = PlayerArchive(str(tmp_path / "player_archive.db"))
        assert await a.get(1) is None
        await a.save(1, "nick", "Nick Name", kind="solo", roles="EXP,GOLD", stars=900)
        p = await a.get(1)
        assert p.roles == ["EXP", "GOLD"] and p.stars == 900 and p.kind == "solo" and p.reg_count == 1

        # joining a team later: roles are kept, stars updated
        await a.save(1, "nick2", "Nick Name", kind="member", stars=1200, team_name="Alpha")
        p = await a.get(1)
        assert p.roles == ["EXP", "GOLD"] and p.stars == 1200 and p.team_name == "Alpha"
        assert p.username == "nick2" and p.reg_count == 2

        # a fresh object on the same file (bot restart / main DB wiped) still sees the player
        b = PlayerArchive(a.path)
        assert (await b.get(1)).stars == 1200
        assert await b.count() == 1

    asyncio.run(run())


def test_broken_path_never_raises(tmp_path):
    async def run():
        a = PlayerArchive(str(tmp_path / "missing_dir" / "x.db"))
        assert await a.get(1) is None
        await a.save(1, None, "", kind="solo", roles="EXP", stars=1)  # logged, not raised

    asyncio.run(run())
