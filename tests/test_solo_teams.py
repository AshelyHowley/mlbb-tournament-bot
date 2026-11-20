"""«Собрать команды из соло»: role coverage, star balance, captain, names, re-assembly, notifications."""
import random
import sqlite3

import pytest
from aiogram.methods import SendMessage

from app.repositories import teams as teams_repo
from app.services.registration import ROLES, RegistrationService, TgUser
from app.services.solo_teams import SoloPlayer, TEAM_NAMES, parse_roles, pick_names, plan_teams
from app.utils.text import new_invite_token
from tests.harness import ADMIN_ID, Harness


def _p(uid, roles, stars, order=None):
    return SoloPlayer(uid, f"user{uid}", f"U{uid}", parse_roles(roles), stars, uid if order is None else order)


# ---------------------------------------------------------------- pure planning
def test_full_teams_one_player_per_role_and_leftovers_are_latest():
    rng = random.Random(1)
    players = [_p(i, ROLES[i % 5], 100 + i * 37 % 500) for i in range(12)]
    plan = plan_teams(players, rng=rng)
    assert len(plan.teams) == 2
    for t in plan.teams:
        assert len(t.seats) == 5
        assert sorted(s.role for s in t.seats) == sorted(ROLES)
        assert not any(s.off_role for s in t.seats)
    assert [p.user_id for p in plan.leftovers] == [10, 11]


def test_captain_has_most_stars():
    plan = plan_teams([_p(i, r, s) for i, (r, s) in enumerate(zip(ROLES, (300, 900, 100, 900, 50)))])
    t = plan.teams[0]
    assert t.captain.user_id == 1  # 900 stars, registered before user 3 (also 900)
    assert t.seats[0].player.user_id == 1


def test_stars_are_balanced():
    rng = random.Random(7)
    players = [_p(i, ROLES[i % 5], rng.randint(0, 2000)) for i in range(40)]
    plan = plan_teams(players, rng=rng)
    totals = [t.total for t in plan.teams]
    assert len(totals) == 8
    best_role_spread = max(max(p.stars for p in players if ROLES[p.user_id % 5] == r)
                           - min(p.stars for p in players if ROLES[p.user_id % 5] == r) for r in ROLES)
    assert max(totals) - min(totals) < best_role_spread / 2


def test_roles_respected_and_earlier_player_wins_a_contested_role():
    # two MID-only players for one MID seat: the earlier one plays, the later one waits
    players = [_p(0, "MID", 10), _p(1, "MID", 9000), _p(2, "EXP", 1), _p(3, "GOLD", 1),
               _p(4, "JUNGLE", 1), _p(5, "ROAM", 1)]
    plan = plan_teams(players)
    assert [p.user_id for p in plan.leftovers] == [1]


def test_flexible_players_move_to_make_room():
    # user 0 can play MID or ROAM; user 5 only MID -> 0 must take ROAM so 5 gets MID
    players = [_p(0, "MID,ROAM", 1), _p(1, "EXP", 1), _p(2, "GOLD", 1), _p(3, "JUNGLE", 1), _p(5, "MID", 1, order=4)]
    plan = plan_teams(players)
    roles = {s.player.user_id: s.role for s in plan.teams[0].seats}
    assert roles[0] == "ROAM" and roles[5] == "MID"
    assert not any(s.off_role for s in plan.teams[0].seats)


def test_all_role_fits_anywhere_and_gaps_filled_off_role():
    plan = plan_teams([_p(0, "ALL", 1)] + [_p(i, "MID", 1) for i in range(1, 5)])
    seats = plan.teams[0].seats
    assert sorted(s.role for s in seats) == sorted(ROLES)
    assert sum(s.off_role for s in seats) == 3  # one MID plays MID, "ALL" takes a role, three play off-role


def test_names_unique_and_skip_taken():
    taken = {n.casefold() for n in TEAM_NAMES[:-2]}
    names = pick_names(4, taken, random.Random(3))
    assert len(set(names)) == 4
    assert not {n.casefold() for n in names} & taken
    assert set(TEAM_NAMES[-2:]) <= set(names)


def test_fewer_than_five_builds_nothing():
    plan = plan_teams([_p(i, "MID", 1) for i in range(4)])
    assert plan.teams == [] and len(plan.leftovers) == 4


# ---------------------------------------------------------------- through the bot


async def _register(pool, n):
    svc = RegistrationService(pool)
    for i in range(n):
        await svc.register_solo(TgUser(100 + i, f"solo_user{i}", f"Solo {i}"), ROLES[i % 5], stars=100 * (i + 1))


def _sent_to(h):
    return [m.chat_id for m in h.tg.requests if isinstance(m, SendMessage) and "Вы попали в команду" in (m.text or "")]


@pytest.mark.db
async def test_button_builds_teams_keeps_solo_and_notifies_only_placed(pool):
    await _register(pool, 12)
    h = Harness(pool)
    await h.press(ADMIN_ID, "admin_user", "rnd:solo")
    assert "Будет собрано команд: <b>2</b>" in h.last()
    h.clear()
    await h.press(ADMIN_ID, "admin_user", "rnd:solo:go")
    assert not h.errors
    out = "\n".join(h.texts())
    assert "Собрано команд: <b>2</b>" in out and "Без места (2)" in out
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams WHERE from_solo = 1") == 2
        assert await c.fetchval("SELECT count(*) FROM team_members") == 10
        assert await c.fetchval("SELECT count(*) FROM solo_players") == 12  # everyone keeps solo registration
        for t in await c.fetch("SELECT id, captain_id FROM teams"):
            top = await c.fetchval("SELECT max(rank) FROM team_members WHERE team_id = $1", t["id"])
            cap = await c.fetchval("SELECT rank FROM team_members WHERE team_id = $1 AND user_id = $2", t["id"], t["captain_id"])
            assert cap == top
    sent = _sent_to(h)
    assert len(sent) == 10 and not {110, 111} & set(sent)  # the two latest got no place and no message
    h.close()


@pytest.mark.db
async def test_reassembly_replaces_previous_teams(pool):
    await _register(pool, 10)
    h = Harness(pool)
    await h.press(ADMIN_ID, "admin_user", "rnd:solo:go")
    await RegistrationService(pool).register_solo(TgUser(200, "late_player", "Late"), "MID", stars=5000)
    await h.press(ADMIN_ID, "admin_user", "rnd:solo")
    assert "будут расформированы" in h.last()
    await h.press(ADMIN_ID, "admin_user", "rnd:solo:go")
    assert not h.errors
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == 2
        assert await c.fetchval("SELECT count(*) FROM team_members") == 10
        assert await c.fetchval("SELECT count(*) FROM solo_players") == 11
    h.close()


@pytest.mark.db
async def test_manual_teams_are_untouched_and_still_exclusive(pool):
    await _register(pool, 5)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO users (telegram_id, username, full_name) VALUES (500, 'manual_cap', 'M')")
        tid = await teams_repo.insert_team(c, "Manual", "manual", 500, 500, new_invite_token(), None)
        await teams_repo.add_member(c, tid, 500, is_captain=True)
        with pytest.raises(sqlite3.IntegrityError):  # a solo player still cannot join an ordinary team
            await teams_repo.add_member(c, tid, 100)
    h = Harness(pool)
    await h.press(ADMIN_ID, "admin_user", "rnd:solo:go")
    await h.press(ADMIN_ID, "admin_user", "rnd:solo:go")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams WHERE name = 'Manual'") == 1
        assert await c.fetchval("SELECT count(*) FROM teams WHERE from_solo = 1") == 1
    h.close()


@pytest.mark.db
async def test_placed_member_unregistering_leaves_team_and_solo(pool):
    await _register(pool, 5)
    h = Harness(pool)
    await h.press(ADMIN_ID, "admin_user", "rnd:solo:go")
    async with pool.acquire() as c:
        cap = await c.fetchval("SELECT captain_id FROM teams")
        member = await c.fetchval("SELECT user_id FROM team_members WHERE user_id <> $1 LIMIT 1", cap)
    await h.press(member, f"solo_user{member - 100}", "me:unregister")
    assert not h.errors
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE user_id = $1", member) == 0
        assert await c.fetchval("SELECT count(*) FROM solo_players WHERE user_id = $1", member) == 0
        assert await c.fetchval("SELECT count(*) FROM team_members") == 4
    h.close()
