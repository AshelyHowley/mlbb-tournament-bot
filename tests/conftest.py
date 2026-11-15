from __future__ import annotations

import asyncio
import os
import sys
import shutil
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.migrate import migrate  # noqa: E402
from app.database.pool import create_pool  # noqa: E402


@pytest.fixture(scope="session")
def db_schema(tmp_path_factory):
    """A migrated SQLite template database, built once per test session.
    Real tournament data (tournament.db) is never touched."""
    template = tmp_path_factory.mktemp("db") / "template.db"

    async def setup():
        pool = await create_pool(f"sqlite:///{template}")
        await migrate(pool)
        async with pool.acquire() as c:
            await c.execute("DELETE FROM tournaments")
            await c.execute("INSERT INTO tournaments (status) VALUES ('active')")
            await c.execute("UPDATE settings SET value = 'true' WHERE key = 'registration_open'")
        await pool.close()

    asyncio.run(setup())
    yield template


@pytest_asyncio.fixture
async def pool(db_schema, tmp_path):
    """Every test gets its own fresh copy of the migrated database."""
    db = tmp_path / "test.db"
    shutil.copyfile(db_schema, db)
    p = await create_pool(f"sqlite:///{db}")
    yield p
    await p.close()
