from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# import asyncpg  # removed for SQLite
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_database_url  # noqa: E402
from app.database.migrate import migrate  # noqa: E402
from app.database.pool import create_pool  # noqa: E402


def _dsn() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    try:
        return load_database_url()
    except ConfigError:
        return None


@pytest.fixture(scope="session")
def db_schema():
    """Tests run in a throw-away schema so real tournament data is never touched."""
    dsn = _dsn()
    if not dsn:
        pytest.skip("no DATABASE_URL / TEST_DATABASE_URL configured - DB tests skipped")
    schema = "test_" + uuid.uuid4().hex[:12]

    async def setup():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
        finally:
            await conn.close()
        pool = await create_pool(dsn, schema=schema, min_size=1, max_size=2)
        try:
            await migrate(pool)
        finally:
            await pool.close()

    async def teardown():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn.close()

    asyncio.run(setup())
    yield dsn, schema
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def pool(db_schema):
    dsn, schema = db_schema
    p = await create_pool(dsn, schema=schema, min_size=2, max_size=12)
    async with p.acquire() as c:
        await c.execute(
            "TRUNCATE broadcasts, result_broadcasts, matches, team_members, teams, solo_players, "
            "tournaments, deputy_invites, deputies, users RESTART IDENTITY CASCADE"
        )
        await c.execute("INSERT INTO tournaments (status) VALUES ('active')")
        await c.execute("UPDATE settings SET value = 'true' WHERE key = 'registration_open'")
        await c.execute("DELETE FROM stages WHERE code NOT IN ('r16','r8','r4','semi','third','final')")
    yield p
    await p.close()
