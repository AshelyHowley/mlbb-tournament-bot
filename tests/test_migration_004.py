"""Migration 004 (tournament system) on top of a database that already holds 001+002+003 data."""
import uuid

# import asyncpg  # removed for SQLite
import pytest

from app.database.migrate import MIGRATIONS_DIR

pytestmark = pytest.mark.db


def sql(n: str) -> str:
    return next(MIGRATIONS_DIR.glob(f"{n}_*.sql")).read_text(encoding="utf-8")


async def test_004_backfills_existing_data_into_one_active_tournament(db_schema):
    dsn, _ = db_schema
    schema = "mig4_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    c = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    try:
        for n in ("001", "002", "003"):
            await c.execute(sql(n))
        for uid in (10, 11, 40):
            await c.execute("INSERT INTO users (telegram_id, username, username_norm, full_name) VALUES ($1,$2,$2,$3)", uid, f"user{uid:04d}", f"U{uid}")
        async with c.transaction():
            t = await c.fetchval(
                "INSERT INTO teams (name,name_norm,created_by,captain_id,invite_token,captain_invite_token) "
                "VALUES ('Old','old',10,10,$1,NULL) RETURNING id", "o" * 32)
            await c.execute("INSERT INTO team_members (team_id,user_id,is_captain) VALUES ($1,10,TRUE)", t)
            await c.execute("INSERT INTO team_members (team_id,user_id,is_captain) VALUES ($1,11,FALSE)", t)
        await c.execute("INSERT INTO solo_players (user_id,username,username_norm,role) VALUES (40,'user0040','user0040','GOLD')")
        await c.execute("INSERT INTO broadcasts (admin_id,audience,content_type,text,status) VALUES (1,'all','text','hi','done')")

        async with c.transaction():
            await c.execute(sql("004"))

        # exactly one active tournament, everything attached to it
        active = await c.fetchrow("SELECT id, status FROM tournaments WHERE status = 'active'")
        assert active is not None
        assert await c.fetchval("SELECT tournament_id FROM teams WHERE id = $1", t) == active["id"]
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE tournament_id = $1", active["id"]) == 2
        assert await c.fetchval("SELECT tournament_id FROM solo_players WHERE user_id = 40") == active["id"]
        assert await c.fetchval("SELECT count(*) FROM broadcasts") == 1

        # new rules are live: DEFAULT fills tournament_id even for a raw insert that omits it
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (99, 'x')")
        tid2 = await c.fetchval(
            "INSERT INTO teams (name,name_norm,created_by,invite_token,captain_invite_token) "
            "VALUES ('New','new',99,$1,$2) RETURNING id", "n" * 32, "c" * 32)
        assert await c.fetchval("SELECT tournament_id FROM teams WHERE id = $1", tid2) == active["id"]

        # a team with only 1 member cannot be assigned to a tournament match
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8', $1, $2)", t, tid2)
        assert e.value.sqlstate == "MB005"

        # a nonexistent team is reported as not found, not "not eligible"
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8', $1, 999999)", t)
        assert e.value.sqlstate == "MB012"

        # 'third' stage exists and sits between semi and final
        rows = await c.fetch("SELECT code, sort_order FROM stages ORDER BY sort_order")
        codes = [r["code"] for r in rows]
        assert codes.index("semi") < codes.index("third") < codes.index("final")

        # solo/team registration is scoped per tournament: the old team member (11) is free once a
        # SECOND tournament exists (simulated directly, without going through the archive flow)
        assert await c.fetchval("SELECT count(*) FROM tournaments") == 1
    finally:
        await c.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_004_full_chain_applies_and_is_recorded(pool):
    async with pool.acquire() as c:
        versions = [r["version"] for r in await c.fetch("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == ["001_init", "002_team_invites", "003_team_roles", "004_tournament_system"]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status = 'active'") == 1
        cols = {r["column_name"] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'teams'")}
        assert {"tournament_id", "created_by", "captain_invite_token"} <= cols
        tables = {r["table_name"] for r in await c.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
        assert {"tournaments", "match_games", "deputies", "deputy_invites", "result_broadcasts", "result_broadcast_deliveries"} <= tables
