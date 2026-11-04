"""Concurrency / latency measurements against PostgreSQL (never against real Telegram)."""
import asyncio
import json
import statistics
import time
from pathlib import Path

import pytest

from app.database.pool import create_pool
from app.services.admin import AdminService
from app.services.deputy import DeputyService
from app.services.registration import RegistrationService, TgUser
from app.services.series import SeriesService
from app.services.team import TeamService
from app.services.tournament import TournamentService
from tests.helpers import add_user, make_team, uname

pytestmark = [pytest.mark.db, pytest.mark.perf]

REPORT = Path(__file__).resolve().parent.parent / "logs" / "perf_report.json"
LEVELS = (10, 20, 50)
_results: dict = {}


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


async def timed(coro):
    t = time.perf_counter()
    try:
        r = await coro
    except Exception as e:  # noqa: BLE001
        r = e
    return (time.perf_counter() - t) * 1000, r


async def run_level(name, n, factory):
    started = time.perf_counter()
    out = await asyncio.gather(*[timed(factory(i)) for i in range(n)])
    wall = (time.perf_counter() - started) * 1000
    lat = [x[0] for x in out]
    stat = {"n": n, "p50_ms": round(statistics.median(lat), 1), "p95_ms": round(pct(lat, 95), 1),
            "max_ms": round(max(lat), 1), "wall_ms": round(wall, 1)}
    _results.setdefault(name, {})[n] = stat
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(_results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[perf] {name:<26} n={n:<3} p50={stat['p50_ms']:>7}ms p95={stat['p95_ms']:>7}ms max={stat['max_ms']:>7}ms")
    return [x[1] for x in out]


@pytest.fixture
async def ppool(db_schema):
    dsn, schema = db_schema
    p = await create_pool(dsn, schema=schema, min_size=2, max_size=10)  # same size as production
    async with p.acquire() as c:
        await c.execute(
            "TRUNCATE broadcasts, result_broadcasts, matches, team_members, teams, solo_players, "
            "tournaments, deputy_invites, deputies, users RESTART IDENTITY CASCADE"
        )
        await c.execute("INSERT INTO tournaments (status) VALUES ('active')")
    # The production pool is long-lived; measure warm behaviour, and report the cold cost separately.
    t = time.perf_counter()
    conns = await asyncio.gather(*[p.acquire() for _ in range(10)])
    _results["pool_warmup_10_connections_ms"] = round((time.perf_counter() - t) * 1000, 1)
    await asyncio.gather(*[p.release(c) for c in conns])
    yield p
    await p.close()


async def seed_500(pool):
    """~500 participants: 60 full teams (300 players) + 200 solo."""
    reg, teams = RegistrationService(pool), TeamService(pool)
    for i in range(1, 61):
        v = await teams.create_team(TgUser(i * 10, uname(i, "cap"), f"Cap {i}"), f"Team {i}")
        for k in range(1, 5):
            await teams.join_team(TgUser(i * 10 + k, uname(i * 10 + k, "mem"), f"M{i}-{k}"), v.invite_token)
    for i in range(1, 201):
        await reg.register_solo(TgUser(100000 + i, uname(i, "solo"), f"S{i}"), ["EXP", "GOLD", "MID", "JUNGLE", "ROAM"][i % 5])
    adm = AdminService(pool)
    all_teams = await adm.all_teams()
    for i in range(0, 20, 2):
        await adm.create_match(all_teams[i]["id"], all_teams[i + 1]["id"], "r16")

@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_team_creation_distinct(ppool, n):
    teams = TeamService(ppool)
    res = await run_level("create_team(distinct)", n, lambda i: teams.create_team(TgUser(1000 + i, uname(i, "cap"), f"C{i}"), f"Perf {i}"))
    assert all(not isinstance(r, Exception) for r in res), [r for r in res if isinstance(r, Exception)][:2]
    async with ppool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == n
        assert await c.fetchval("SELECT count(*) FROM team_members") == n


@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_duplicate_team_creation(ppool, n):
    teams = TeamService(ppool)
    res = await run_level("create_team(duplicate)", n, lambda i: teams.create_team(TgUser(1, "capone01", "C"), "Dup"))
    assert sum(not isinstance(r, Exception) for r in res) == 1
    async with ppool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == 1


@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_joins_for_last_seat_never_exceed_five(ppool, n):
    teams = TeamService(ppool)
    v = await teams.create_team(TgUser(1, "capone01", "C"), "Full Soon")
    for k in range(2, 5):
        await teams.join_team(TgUser(k, None, f"P{k}"), v.invite_token)  # 4/5
    res = await run_level("join_team(last seat)", n, lambda i: teams.join_team(TgUser(5000 + i, None, f"J{i}"), v.invite_token))
    assert sum(not isinstance(r, Exception) for r in res) == 1
    async with ppool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5


@pytest.mark.parametrize("n", LEVELS)
async def test_captain_accept_vs_player_join_race_at_four_of_five(ppool, n):
    """Captain-less team holding 4 players (raw SQL state); half the requests accept the captain link, half join as players."""
    async with ppool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (1,'Reg')")
        tid = await c.fetchval("INSERT INTO teams (name,name_norm,created_by,invite_token,captain_invite_token) "
                               "VALUES ('Race','race',1,$1,$2) RETURNING id", "P" * 32, "C" * 32)
        for i in range(4):
            await c.execute("INSERT INTO users (telegram_id, full_name) VALUES ($1,'m')", 10 + i)
            await c.execute("INSERT INTO team_members (team_id,user_id) VALUES ($1,$2)", tid, 10 + i)
    teams = TeamService(ppool)

    def op(i):
        if i % 2:
            return teams.accept_captain(TgUser(1000 + i, None, f"C{i}"), "C" * 32)
        return teams.join_team(TgUser(5000 + i, None, f"J{i}"), "P" * 32)

    res = await run_level("captain-accept vs join(4/5)", n, op)
    assert all(not isinstance(r, Exception) or hasattr(r, "user_message") for r in res), [r for r in res if isinstance(r, Exception) and not hasattr(r, "user_message")][:2]
    async with ppool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE is_captain") == 1

@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_joins_across_many_teams(ppool, n):
    """n different players join n different teams at once (the 500-participant registration rush)."""
    teams = TeamService(ppool)
    views = [await teams.create_team(TgUser(1000 + i, None, f"C{i}"), f"Rush {i}") for i in range(n)]
    res = await run_level("join_team(distinct teams)", n, lambda i: teams.join_team(TgUser(9000 + i, None, f"J{i}"), views[i].invite_token))
    assert all(not isinstance(r, Exception) for r in res), [r for r in res if isinstance(r, Exception)][:2]

@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_solo_registrations(ppool, n):
    reg = RegistrationService(ppool)
    res = await run_level("register_solo(distinct)", n, lambda i: reg.register_solo(TgUser(2000 + i, uname(i, "solo")), "MID"))
    assert all(not isinstance(r, Exception) for r in res)


@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_reads_with_500_participants(ppool, n):
    await seed_500(ppool)
    adm, reg = AdminService(ppool), RegistrationService(ppool)
    ops = [lambda i: adm.teams_page(i % 8), lambda i: adm.solo_page(i % 25), lambda i: adm.bracket_rows(),
           lambda i: reg.my_registration(((i % 60) + 1) * 10), lambda i: adm.team_card(1 + i % 60)]
    names = ["admin.teams_page", "admin.solo_page", "bracket_rows", "my_registration", "team_card"]
    for name, op in zip(names, ops):
        res = await run_level(name, n, op)
        assert all(not isinstance(r, Exception) for r in res), [r for r in res if isinstance(r, Exception)][:2]
    # p95 of the slowest read stays comfortably below Telegram's callback-answer window
    worst = max(_results[nm][n]["p95_ms"] for nm in names)
    assert worst < 1500, worst


@pytest.mark.parametrize("n", LEVELS)
async def test_mixed_load_registrations_and_reads(ppool, n):
    await seed_500(ppool)
    reg, adm = RegistrationService(ppool), AdminService(ppool)

    def op(i):
        if i % 3 == 0:
            return reg.register_solo(TgUser(300000 + i, uname(i, "mix")), "ROAM")
        if i % 3 == 1:
            return adm.bracket_rows()
        return adm.teams_page(0)

    res = await run_level("mixed(reg+reads)", n, op)
    assert all(not isinstance(r, Exception) for r in res)


async def test_key_lookups_use_indexes(ppool):
    await seed_500(ppool)
    queries = {
        "team by captain": ("SELECT * FROM teams WHERE captain_id = 10", ),
        "membership by user": ("SELECT * FROM team_members WHERE user_id = 11", ),
        "team by invite token": ("SELECT * FROM teams WHERE invite_token = 'x'", ),
        "members of team": ("SELECT * FROM team_members WHERE team_id = 1", ),
        "solo by user": ("SELECT * FROM solo_players WHERE user_id = 100001", ),
        "match by team": ("SELECT * FROM matches WHERE team_a_id = 1 OR team_b_id = 1", ),
        "user by username": ("SELECT * FROM users WHERE username_norm = 'solo0001'", ),
        "team by name": ("SELECT id FROM teams WHERE name_norm = 'team 5'", ),
    }
    async with ppool.acquire() as c:
        await c.execute("ANALYZE")
        await c.execute("SET enable_seqscan = off")  # proves an index *exists* and is usable
        for label, (sql,) in queries.items():
            plan = "\n".join(r[0] for r in await c.fetch("EXPLAIN " + sql))
            assert "Seq Scan" not in plan, f"{label}: {plan}"


async def test_pool_has_no_leaks_after_load(ppool):
    reg = RegistrationService(ppool)
    await asyncio.gather(*[reg.my_registration(i) for i in range(200)])
    assert ppool.get_idle_size() == ppool.get_size()  # every connection returned


class FakeBot:
    """Enough of aiogram.Bot for SeriesService's best-effort result notifications."""

    async def send_message(self, chat_id, text, **kw):
        pass

    async def send_photo(self, chat_id, photo, **kw):
        pass

    async def send_video(self, chat_id, video, **kw):
        pass

    async def send_document(self, chat_id, document, **kw):
        pass


@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_bo5_game_submissions(ppool, n):
    """n admins race to record the SAME game result for a fresh BO5 series; the DB must accept
    exactly one game per real "slot" and never let the series exceed 5 games or flip once decided."""
    a, b = await make_team(ppool, 1), await make_team(ppool, 2, size=2)
    adm, series = AdminService(ppool), SeriesService(ppool, FakeBot())
    mid = await adm.create_match(a, b, "r4")
    actor = TgUser(1, "actor0001", "Actor")
    res = await run_level("series.add_game(race)", n, lambda i: series.add_game(actor, mid, a if i % 2 else b))
    ok = [r for r in res if not isinstance(r, Exception)]
    async with ppool.acquire() as c:
        played = await c.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", mid)
    assert played <= 5
    assert len(ok) == played
    assert all(getattr(r, "user_message", None) for r in res if isinstance(r, Exception))


@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_deputy_invite_claims(ppool, n):
    """n distinct candidates race to claim one one-candidate invite token; exactly one may win it."""
    await add_user(ppool, 1, "mainadmin")
    svc = DeputyService(ppool)
    token = await svc.create_invite(1)
    candidates = [TgUser(9000 + i, uname(i, "dep"), f"D{i}") for i in range(n)]
    res = await run_level("deputy.claim(race)", n, lambda i: svc.claim(candidates[i], token))
    winners = {r["candidate_id"] for r in res if not isinstance(r, Exception)}
    assert len(winners) == 1
    assert all(getattr(r, "user_message", None) for r in res if isinstance(r, Exception))


@pytest.mark.parametrize("n", LEVELS)
async def test_concurrent_tournament_finalize(ppool, n):
    """n staff members (main admin + deputies) race to finish the same tournament at once. Since
    each winner immediately opens a fresh active tournament, a caller queued behind the pool's
    connection limit may legitimately finalize *that* one next - real multi-round completion, not
    a bug. What must never happen: two callers archiving the same tournament row, or ending up with
    zero or two active rows. `force=True` since these throwaway tournaments have no placements."""
    await add_user(ppool, 1, "mainadmin")
    svc = TournamentService(ppool)
    actor = TgUser(1, "mainadmin", "Admin")
    res = await run_level("tournament.finalize(race)", n, lambda i: svc.finalize(actor, force=True))
    ok = [r for r in res if not isinstance(r, Exception)]
    assert 1 <= len(ok) <= n
    archived_ids = [tid for tid, _placements in ok]
    assert len(set(archived_ids)) == len(archived_ids)  # no tournament row archived twice
    assert all(getattr(r, "user_message", None) for r in res if isinstance(r, Exception))
    async with ppool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status = 'active'") == 1
        assert await c.fetchval(
            "SELECT count(*) FROM tournaments WHERE status IN ('completed','closed_early')"
        ) == len(ok)
