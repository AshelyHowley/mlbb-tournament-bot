"""Migration 002 must upgrade a database that already holds 001-era data without losing anything."""
import uuid

# import asyncpg  # removed for SQLite
import pytest

from app.database.migrate import MIGRATIONS_DIR

pytestmark = pytest.mark.db


def sql(name: str) -> str:
    return next(MIGRATIONS_DIR.glob(f"{name}_*.sql")).read_text(encoding="utf-8")


async def test_upgrade_preserves_legacy_teams_solo_and_archives_unverifiable_members(db_schema):
    dsn, _ = db_schema
    schema = "mig_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    c = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    try:
        await c.execute(sql("001"))
        # --- legacy (username based) data --------------------------------------------------------
        for uid, un in [(10, "cap0001"), (11, "mem0011"), (20, "cap0002"), (40, "solo0040"), (50, "mem0013"), (51, "mem0013")]:
            await c.execute("INSERT INTO users (telegram_id, username, username_norm, full_name) VALUES ($1,$2,$2,$3)", uid, un, f"U{uid}")
        async with c.transaction():
            t1 = await c.fetchval("INSERT INTO teams (name,name_norm,captain_id,captain_username_norm) VALUES ('Old One','old one',10,'cap0001') RETURNING id")
            for pos, un in enumerate(["cap0001", "mem0011", "mem0012", "mem0013", "mem0014"], 1):
                await c.execute("INSERT INTO team_members (team_id,position,username,username_norm) VALUES ($1,$2,$3,$3)", t1, pos, un)
            t2 = await c.fetchval("INSERT INTO teams (name,name_norm,captain_id,captain_username_norm) VALUES ('Old Two','old two',20,'cap0002') RETURNING id")
            for pos, un in enumerate(["cap0002", "ghost001", "ghost002", "ghost003", "ghost004"], 1):
                await c.execute("INSERT INTO team_members (team_id,position,username,username_norm) VALUES ($1,$2,$3,$3)", t2, pos, un)
        await c.execute("INSERT INTO solo_players (user_id,username,username_norm,role) VALUES (40,'solo0040','solo0040','MID')")
        await c.execute("INSERT INTO broadcasts (admin_id,audience,content_type,text,status) VALUES (1,'all','text','hi','done')")
        await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8',$1,$2)", t1, t2)

        # --- upgrade -----------------------------------------------------------------------------
        async with c.transaction():
            await c.execute(sql("002"))

        # teams survive, with fresh unique tokens
        teams = await c.fetch("SELECT id, name, captain_id, invite_token FROM teams ORDER BY id")
        assert [t["name"] for t in teams] == ["Old One", "Old Two"]
        assert len({t["invite_token"] for t in teams}) == 2 and all(len(t["invite_token"]) == 32 for t in teams)
        # verifiable people are members by user_id; unverifiable username-only rows left the live roster
        live = await c.fetch("SELECT team_id, user_id, is_captain FROM team_members ORDER BY team_id, user_id")
        assert [(r["team_id"], r["user_id"], r["is_captain"]) for r in live] == [(t1, 10, True), (t1, 11, False), (t2, 20, True)]
        # ...but nothing was destroyed: the whole old roster is archived
        legacy = await c.fetch("SELECT username FROM team_members_legacy ORDER BY team_id, position")
        assert len(legacy) == 10 and "ghost003" in [r["username"] for r in legacy] and "mem0013" in [r["username"] for r in legacy]
        # other tables untouched
        assert await c.fetchval("SELECT count(*) FROM matches") == 1
        assert await c.fetchval("SELECT count(*) FROM broadcasts") == 1
        solo = await c.fetchrow("SELECT user_id, username, role FROM solo_players")
        assert (solo["user_id"], solo["username"], solo["role"]) == (40, "solo0040", "MID")

        # --- new rules are live on the upgraded database -----------------------------------------
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (60, 'no username')")
        await c.execute("INSERT INTO solo_players (user_id, role) VALUES (60, 'EXP')")            # username optional
        for uid in (71, 72, 73):
            await c.execute("INSERT INTO users (telegram_id, full_name) VALUES ($1, 'x')", uid)
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, $2)", t1, uid)  # 3 -> 5/5
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (74, 'six')")
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 74)", t1)
        assert e.value.sqlstate == "MB003"
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 71)", t2)  # already in t1
        with pytest.raises(asyncpg.PostgresError):
            await c.execute("DELETE FROM team_members WHERE user_id = 10")  # captain
        old_cols = await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = 'team_members'", schema)
        assert {r["column_name"] for r in old_cols} >= {"user_id", "is_captain", "team_id"}
        assert not ({"username", "username_norm", "position"} & {r["column_name"] for r in old_cols})
    finally:
        await c.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_migration_is_recorded_and_idempotent_via_runner(pool):
    async with pool.acquire() as c:
        versions = [r["version"] for r in await c.fetch("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == ["001_init", "002_team_invites", "003_team_roles", "004_tournament_system"]
    from app.database.migrate import migrate

    assert await migrate(pool) == []  # second run applies nothing
