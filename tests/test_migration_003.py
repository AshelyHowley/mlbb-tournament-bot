"""Migration 003 (team roles) on top of a database that already holds 001+002 data."""
import uuid

# import asyncpg  # removed for SQLite
import pytest

from app.database.migrate import MIGRATIONS_DIR

pytestmark = pytest.mark.db


def sql(n: str) -> str:
    return next(MIGRATIONS_DIR.glob(f"{n}_*.sql")).read_text(encoding="utf-8")


async def test_003_preserves_existing_teams_and_enables_pending_captain(db_schema):
    dsn, _ = db_schema
    schema = "mig_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    c = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    try:
        await c.execute(sql("001"))
        await c.execute(sql("002"))
        for uid in (10, 11, 12, 40):
            await c.execute("INSERT INTO users (telegram_id, username, username_norm, full_name) VALUES ($1,$2,$2,$3)", uid, f"user{uid:04d}", f"U{uid}")
        async with c.transaction():   # a 002-era team with a captain and two members
            t = await c.fetchval(
                "INSERT INTO teams (name,name_norm,captain_id,invite_token) VALUES ('Old','old',10,$1) RETURNING id", "o" * 32)
            for uid, cap in ((10, True), (11, False), (12, False)):
                await c.execute("INSERT INTO team_members (team_id,user_id,is_captain) VALUES ($1,$2,$3)", t, uid, cap)
        await c.execute("INSERT INTO solo_players (user_id,username,username_norm,role) VALUES (40,'user0040','user0040','GOLD')")
        await c.execute("INSERT INTO broadcasts (admin_id,audience,content_type,text,status) VALUES (1,'all','text','hi','done')")
        await c.execute("UPDATE settings SET value='false' WHERE key='registration_open'")

        async with c.transaction():
            await c.execute(sql("003"))

        row = await c.fetchrow("SELECT captain_id, created_by, captain_invite_token FROM teams")
        assert (row["captain_id"], row["created_by"], row["captain_invite_token"]) == (10, 10, None)   # registrar = old captain
        assert await c.fetchval("SELECT count(*) FROM team_members") == 3
        assert await c.fetchval("SELECT role FROM solo_players WHERE user_id = 40") == "GOLD"
        assert await c.fetchval("SELECT count(*) FROM broadcasts") == 1
        assert await c.fetchval("SELECT value FROM settings WHERE key='registration_open'") == "false"
        assert await c.fetchval("SELECT count(*) FROM stages") == 5

        # new capabilities: registrar-only, captain pending, invariants
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (50,'Registrar')")
        p = await c.fetchval(
            "INSERT INTO teams (name,name_norm,created_by,invite_token,captain_invite_token) VALUES ('Pend','pend',50,$1,$2) RETURNING id",
            "p" * 32, "c" * 32)
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("UPDATE teams SET captain_invite_token = invite_token WHERE id = $1", p)
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("UPDATE team_members SET is_captain = TRUE WHERE user_id = 11")   # captain flag on a team that has one -> unique
        assert e.value.sqlstate in ("23505", "MB002")
        with pytest.raises(asyncpg.PostgresError) as e:
            async with c.transaction():
                await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (51,'x')")
                await c.execute("INSERT INTO team_members (team_id,user_id,is_captain) VALUES ($1,51,TRUE)", p)  # flagged while none assigned
        assert e.value.sqlstate == "MB002"
        # 002 rules still hold: 4 more members fit (3 + 2 = 5), a 6th does not
        for uid in (60, 61):
            await c.execute("INSERT INTO users (telegram_id, full_name) VALUES ($1,'x')", uid)
            await c.execute("INSERT INTO team_members (team_id,user_id) VALUES ($1,$2)", t, uid)
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (62,'x')")
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO team_members (team_id,user_id) VALUES ($1,62)", t)
        assert e.value.sqlstate == "MB003"
    finally:
        await c.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_003_alone_after_001_is_not_possible_but_full_chain_applies_cleanly(pool):
    async with pool.acquire() as c:
        cols = {r["column_name"] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'teams'")}
    assert {"created_by", "captain_invite_token", "invite_token", "captain_id"} <= cols and "captain_username_norm" not in cols
