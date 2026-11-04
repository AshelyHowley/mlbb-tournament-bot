"""Registrar / captain / member roles, captain invites, captain transfer, limits and races (service + DB level)."""
import asyncio

# import asyncpg  # removed for SQLite
import pytest

from app.services.admin import AdminService
from app.services.errors import (
    AlreadyInOtherTeam,
    AlreadyRegistered,
    CaptainExists,
    CaptainRequired,
    Conflict,
    InvalidInvite,
    NotCaptain,
    NotFound,
    RegistrationClosed,
    ServiceError,
    TeamFull,
    ValidationError,
)
from app.services.registration import RegistrationService, TgUser
from app.services.team import TeamService
from tests.helpers import add_user, make_team

pytestmark = pytest.mark.db

REG = TgUser(1, "regist01", "Registrar")     # the person who registers the team
CAPT = TgUser(2, "captain2", "Captain Two")
NEWCAP = TgUser(3, None, "No Username Cap")


def player(i: int) -> TgUser:
    return TgUser(100 + i, None if i % 2 else f"player{i:03d}", f"Player {i}")


async def db_members(pool, team_id=None):
    async with pool.acquire() as c:
        if team_id:
            return await c.fetchval("SELECT count(*) FROM team_members WHERE team_id=$1", team_id)
        return await c.fetchval("SELECT count(*) FROM team_members")


async def assert_roster_valid(pool):
    """The invariants that must hold after ANY sequence of operations."""
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT max(n) FROM (SELECT count(*) n FROM team_members GROUP BY team_id) s") in (None, 1, 2, 3, 4, 5)
        assert await c.fetchval(
            "SELECT count(*) FROM teams t WHERE t.captain_id IS NOT NULL AND (SELECT count(*) FROM team_members m "
            "WHERE m.team_id=t.id AND m.is_captain AND m.user_id=t.captain_id) <> 1") == 0
        assert await c.fetchval(
            "SELECT count(*) FROM teams t WHERE t.captain_id IS NULL AND EXISTS (SELECT 1 FROM team_members m "
            "WHERE m.team_id=t.id AND m.is_captain)") == 0
        assert await c.fetchval("SELECT count(*) FROM (SELECT user_id FROM team_members GROUP BY user_id HAVING count(*)>1) s") == 0


# ---------------------------------------------------------------- 1-4: creator roles
async def test_creator_is_captain(pool):
    v = await TeamService(pool).create_team(CAPT, "Alpha", "captain")
    assert v.size == 1 and v.captain_id == CAPT.id and v.created_by == CAPT.id
    assert v.role_of(CAPT.id) == "captain" and v.captain_invite_token is None
    assert v.captain.user_id == CAPT.id


async def test_creator_is_ordinary_member_captain_pending(pool):
    v = await TeamService(pool).create_team(REG, "Alpha", "member")
    assert v.size == 1 and v.captain_id is None and v.captain_pending
    assert v.created_by == REG.id and v.role_of(REG.id) == "member" and not v.members[0].is_captain
    assert v.captain_invite_token and v.captain_invite_token != v.invite_token
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT is_captain FROM team_members") is False


async def test_creator_is_registrar_only_and_takes_no_slot(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    assert v.size == 0 and v.free == 5 and v.captain_pending and v.role_of(REG.id) == "registrar"
    assert await db_members(pool) == 0
    assert (await RegistrationService(pool).my_registration(REG.id)).team_role == "registrar"
    assert not (await RegistrationService(pool).my_registration(REG.id)).plays
    assert (await svc.view_for_user(REG.id)).team_id == v.team_id
    await assert_roster_valid(pool)


async def test_invalid_creator_role(pool):
    with pytest.raises(ValidationError):
        await TeamService(pool).create_team(REG, "Alpha", "owner")
    with pytest.raises(ValidationError):
        await TeamService(pool).create_team(REG, "A", "captain")


# ---------------------------------------------------------------- 10: limit examples A / B / C (never 6/5)
async def test_limit_case_a_creator_is_captain(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAPT, "Team A", "captain")
    for i in range(4):
        await svc.join_team(player(i), v.invite_token)
    assert (await svc.view_for_user(CAPT.id)).size == 5
    with pytest.raises(TeamFull):
        await svc.join_team(player(9), v.invite_token)


async def test_limit_case_b_creator_member_plus_captain_plus_three(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Team B", "member")
    await svc.accept_captain(CAPT, v.captain_invite_token)
    for i in range(3):
        await svc.join_team(player(i), v.invite_token)
    view = await svc.view_for_user(REG.id)
    assert view.size == 5 and view.role_of(REG.id) == "member" and view.captain_id == CAPT.id
    with pytest.raises(TeamFull):
        await svc.join_team(player(9), v.invite_token)
    await assert_roster_valid(pool)


async def test_limit_case_c_registrar_only_captain_plus_four(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Team C", "registrar")
    await svc.accept_captain(CAPT, v.captain_invite_token)
    for i in range(4):
        await svc.join_team(player(i), v.invite_token)
    view = await svc.registrar_view(REG.id)
    assert view.size == 5 and view.role_of(REG.id) == "registrar"  # registrar not counted, not a player
    assert all(m.user_id != REG.id for m in view.members)
    with pytest.raises(TeamFull):
        await svc.join_team(player(9), v.invite_token)
    await assert_roster_valid(pool)


# ---------------------------------------------------------------- 5-9: captain invite
async def test_captain_accepts_and_becomes_member_and_captain(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    peek = await svc.peek_captain_invite(CAPT.id, v.captain_invite_token)
    assert peek.status == "ok" and peek.team.size == 0
    r = await svc.accept_captain(CAPT, v.captain_invite_token)
    assert not r.already_captain and not r.was_member and r.team.size == 1
    assert r.team.captain_id == CAPT.id and r.team.captain.user_id == CAPT.id and r.team.role_of(CAPT.id) == "captain"
    async with pool.acquire() as c:
        assert (await c.fetchrow("SELECT is_captain FROM team_members WHERE user_id=$1", CAPT.id))["is_captain"] is True
    await assert_roster_valid(pool)


async def test_existing_member_becomes_captain_without_duplicate(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "member")     # creator plays, captain pending
    r = await svc.accept_captain(REG, v.captain_invite_token)  # creator later takes captaincy himself
    assert r.was_member and r.team.size == 1 and r.team.captain_id == REG.id
    assert await db_members(pool) == 1
    await assert_roster_valid(pool)


async def test_member_promoted_via_captain_link_no_seat_used(pool):
    """A plain member of the team who receives the captain link is promoted; size unchanged."""
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "member")
    async with pool.acquire() as c:  # give the pending team a second member by raw SQL (players are gated by captain)
        await add_user(pool, 50, "extra001")
        await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 50)", v.team_id)
    r = await svc.accept_captain(TgUser(50, "extra001", "Extra"), v.captain_invite_token)
    assert r.was_member and r.team.size == 2 and r.team.captain_id == 50
    await assert_roster_valid(pool)


@pytest.mark.parametrize("bad", ["", "short", "x" * 49, "a" * 32 + "\n", "a'; DROP TABLE teams;--" + "a" * 20, "%" * 32, None])
async def test_invalid_captain_token(pool, bad):
    svc = TeamService(pool)
    await svc.create_team(REG, "Alpha", "registrar")
    with pytest.raises(InvalidInvite):
        await svc.accept_captain(CAPT, bad)
    assert (await svc.peek_captain_invite(CAPT.id, bad)).status == "invalid"
    assert await db_members(pool) == 0


async def test_unknown_wellformed_captain_token_and_player_token_is_not_a_captain_token(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    with pytest.raises(InvalidInvite):
        await svc.accept_captain(CAPT, "A" * 32)
    with pytest.raises(InvalidInvite):  # the two link types are not interchangeable
        await svc.accept_captain(CAPT, v.invite_token)
    with pytest.raises(InvalidInvite):
        await svc.join_team(CAPT, v.captain_invite_token)


async def test_captain_tokens_are_random_unique_and_db_enforced(pool):
    svc = TeamService(pool)
    toks = set()
    for i in range(1, 8):
        v = await svc.create_team(TgUser(i, None, f"R{i}"), f"T{i}", "registrar")
        assert len(v.captain_invite_token) == 32 and v.captain_invite_token != v.invite_token
        toks.add(v.captain_invite_token)
    assert len(toks) == 7
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("UPDATE teams SET captain_invite_token = $1 WHERE id = 1", v.captain_invite_token)
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("UPDATE teams SET captain_invite_token = 'short' WHERE id = 1")
        with pytest.raises(asyncpg.CheckViolationError):  # captain token must differ from the player token
            await c.execute("UPDATE teams SET captain_invite_token = invite_token WHERE id = 1")


# ---------------------------------------------------------------- 8, 10-13: token lifecycle
async def test_captain_link_dies_after_assignment(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    await svc.accept_captain(CAPT, v.captain_invite_token)
    with pytest.raises(CaptainExists) as e:
        await svc.accept_captain(NEWCAP, v.captain_invite_token)
    assert "уже назначен капитан" in e.value.user_message
    assert (await svc.peek_captain_invite(NEWCAP.id, v.captain_invite_token)).status == "captain_exists"
    again = await svc.accept_captain(CAPT, v.captain_invite_token)  # the captain re-opening the link: idempotent
    assert again.already_captain and again.team.size == 1
    assert await db_members(pool) == 1


async def test_regenerate_captain_link_invalidates_old_one(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    old = v.captain_invite_token
    v2 = await svc.regenerate_captain_token(REG.id)
    assert v2.captain_invite_token != old and len(v2.captain_invite_token) == 32
    with pytest.raises(InvalidInvite):
        await svc.accept_captain(CAPT, old)                 # old deep-link: dead
    assert (await svc.peek_captain_invite(CAPT.id, old)).status == "invalid"
    r = await svc.accept_captain(CAPT, v2.captain_invite_token)
    assert r.team.captain_id == CAPT.id
    with pytest.raises(CaptainExists):                       # cannot rotate once a captain exists
        await svc.regenerate_captain_token(REG.id)


async def test_regenerate_is_registrar_only_and_works_for_creator_member(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "member")
    with pytest.raises(NotFound):
        await svc.regenerate_captain_token(CAPT.id)          # a stranger has no registration to rotate
    assert (await svc.regenerate_captain_token(REG.id)).captain_invite_token != v.captain_invite_token


async def test_concurrent_regenerations_leave_exactly_one_valid_token(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    res = await asyncio.gather(*[svc.regenerate_captain_token(REG.id) for _ in range(20)], return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in res)
    async with pool.acquire() as c:
        cur = await c.fetchval("SELECT captain_invite_token FROM teams")
    assert cur in {r.captain_invite_token for r in res} and cur != v.captain_invite_token
    assert (await svc.peek_captain_invite(CAPT.id, cur)).status == "ok"


async def test_player_invite_gated_until_captain_assigned(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "member")
    with pytest.raises(CaptainRequired):
        await svc.join_team(player(1), v.invite_token)
    assert (await svc.peek_invite(player(1).id, v.invite_token)).status == "no_captain"
    assert (await svc.join_team(REG, v.invite_token)).already_member  # the creator-member can still open it
    await svc.accept_captain(CAPT, v.captain_invite_token)
    assert (await svc.join_team(player(1), v.invite_token)).team.size == 3


# ---------------------------------------------------------------- 14-17: limits & races at 4/5
async def raw_pending_team(pool, members: int, name="Raw"):
    """A captain-less team holding `members` players, built with raw SQL (the service never allows this state)."""
    async with pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (9000,'Reg') ON CONFLICT DO NOTHING")
        tid = await c.fetchval(
            "INSERT INTO teams (name,name_norm,created_by,invite_token,captain_invite_token) VALUES ($1,$2,9000,$3,$4) RETURNING id",
            name, name.lower(), "P" * 32, "C" * 32)
        for i in range(members):
            await c.execute("INSERT INTO users (telegram_id, full_name) VALUES ($1,'raw') ON CONFLICT DO NOTHING", 9100 + i)
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1,$2)", tid, 9100 + i)
    return tid, "P" * 32, "C" * 32


async def test_captain_joins_at_four_of_five(pool):
    tid, ptok, ctok = await raw_pending_team(pool, 4)
    r = await TeamService(pool).accept_captain(NEWCAP, ctok)
    assert r.team.size == 5 and r.team.captain_id == NEWCAP.id and not r.was_member
    await assert_roster_valid(pool)


async def test_captain_cannot_join_at_five_of_five_but_member_can_be_promoted(pool):
    tid, ptok, ctok = await raw_pending_team(pool, 5)
    svc = TeamService(pool)
    with pytest.raises(TeamFull):
        await svc.accept_captain(NEWCAP, ctok)
    assert await db_members(pool, tid) == 5
    r = await svc.accept_captain(TgUser(9100, None, "raw"), ctok)  # existing member: promotion needs no seat
    assert r.was_member and r.team.size == 5 and r.team.captain_id == 9100
    await assert_roster_valid(pool)


@pytest.mark.parametrize("n", [10, 20, 50])
async def test_captain_accept_and_player_joins_race_at_four_of_five(pool, n):
    tid, ptok, ctok = await raw_pending_team(pool, 4)
    svc = TeamService(pool)
    ops = []
    for i in range(n):
        if i % 2:
            ops.append(svc.accept_captain(TgUser(20000 + i, None, f"C{i}"), ctok))
        else:
            ops.append(svc.join_team(TgUser(30000 + i, None, f"J{i}"), ptok))
    res = await asyncio.gather(*ops, return_exceptions=True)
    assert all(not isinstance(r, BaseException) or isinstance(r, ServiceError) for r in res), res  # no crash/deadlock
    assert await db_members(pool, tid) == 5                       # exactly the last seat, never 6
    accepted = [r for r in res if not isinstance(r, BaseException) and hasattr(r, "already_captain")]
    joined = [r for r in res if not isinstance(r, BaseException) and hasattr(r, "already_member")]
    assert len(accepted) == 1 and not joined                     # the captain took the seat; players were gated / full
    await assert_roster_valid(pool)


@pytest.mark.parametrize("n", [10, 20, 50])
async def test_players_race_after_captain_exists_at_four_of_five(pool, n):
    svc = TeamService(pool)
    v = await svc.create_team(CAPT, "Alpha", "captain")
    for i in range(3):
        await svc.join_team(player(i), v.invite_token)   # 4/5
    res = await asyncio.gather(*[svc.join_team(TgUser(40000 + i, None, f"J{i}"), v.invite_token) for i in range(n)], return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, TeamFull) for r in res if isinstance(r, BaseException))
    assert await db_members(pool) == 5


async def test_two_captain_acceptors_only_one_wins(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    res = await asyncio.gather(*[svc.accept_captain(TgUser(500 + i, None, f"C{i}"), v.captain_invite_token) for i in range(30)], return_exceptions=True)
    ok = [r for r in res if not isinstance(r, BaseException)]
    assert len(ok) == 1 and all(isinstance(r, CaptainExists) for r in res if isinstance(r, BaseException))
    assert await db_members(pool) == 1
    await assert_roster_valid(pool)


async def test_same_user_accepting_captain_twice_concurrently(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    res = await asyncio.gather(*[svc.accept_captain(CAPT, v.captain_invite_token) for _ in range(20)])
    assert sum(not r.already_captain for r in res) == 1
    assert await db_members(pool) == 1


# ---------------------------------------------------------------- 18-20: permissions
async def test_registrar_only_has_no_roster_rights(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    await svc.accept_captain(CAPT, v.captain_invite_token)
    await svc.join_team(player(1), v.invite_token)
    with pytest.raises(NotCaptain):
        await svc.remove_member(REG.id, player(1).id)
    with pytest.raises(NotCaptain):
        await svc.remove_member(REG.id, CAPT.id)
    with pytest.raises(NotCaptain):
        await svc.transfer_captaincy(REG.id, player(1).id)
    with pytest.raises(NotFound):
        await svc.leave_team(REG.id)                        # not a player: nothing to leave
    with pytest.raises(CaptainExists):
        await svc.regenerate_captain_token(REG.id)
    with pytest.raises(Conflict):
        await svc.cancel_registration(REG.id)               # captain assigned: control passed on
    assert (await svc.registrar_view(REG.id)).size == 2


async def test_member_has_no_roster_rights(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAPT, "Alpha", "captain")
    for i in range(3):
        await svc.join_team(player(i), v.invite_token)
    with pytest.raises(NotCaptain):
        await svc.remove_member(player(0).id, player(1).id)
    with pytest.raises(NotCaptain):
        await svc.transfer_captaincy(player(0).id, player(1).id)
    with pytest.raises(NotCaptain):
        await svc.transfer_captaincy(player(0).id, player(0).id)
    assert (await svc.view_for_user(CAPT.id)).size == 4


async def test_creator_member_is_only_a_member_after_captain_assigned(pool):
    svc = TeamService(pool)
    v = await svc.create_team(REG, "Alpha", "member")
    await svc.accept_captain(CAPT, v.captain_invite_token)
    with pytest.raises(NotCaptain):
        await svc.remove_member(REG.id, CAPT.id)
    with pytest.raises(CaptainExists):
        await svc.regenerate_captain_token(REG.id)
    me, view = await svc.leave_team(REG.id)                 # ordinary member may leave; team stays with its captain
    assert view.size == 1 and view.captain_id == CAPT.id
    assert (await svc.registrar_view(REG.id)).created_by == REG.id  # ...but is still the registrar of record


async def test_captain_permissions_remove_replace_invite(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    cap = 10
    removed, view = await svc.remove_member(cap, 12)
    assert view.size == 4 and removed.user_id == 12
    with pytest.raises(Conflict):
        await svc.remove_member(cap, cap)                    # cannot remove self
    r = await svc.join_team(TgUser(777, None, "Replacement"), view.invite_token)   # same player link, 4/5 -> 5/5
    assert r.team.size == 5
    await assert_roster_valid(pool)


# ---------------------------------------------------------------- 21-24: captain transfer
async def test_captain_transfer_keeps_size_and_swaps_roles(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)                                 # 5/5, captain id 10, members 11..14
    new, old, view = await svc.transfer_captaincy(10, 13)
    assert (new.user_id, old.user_id) == (13, 10) and view.size == 5 and view.captain_id == 13
    assert view.role_of(13) == "captain" and view.role_of(10) == "member"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE is_captain") == 1
        assert await c.fetchval("SELECT captain_id FROM teams") == 13
    with pytest.raises(NotCaptain):
        await svc.transfer_captaincy(10, 11)                 # the old captain has no rights any more
    removed, v2 = await svc.remove_member(13, 10)            # new captain manages the roster
    assert v2.size == 4
    await assert_roster_valid(pool)


@pytest.mark.parametrize("target", ["outsider", "other_team", "registrar", "self", "captain_of_other", "nonexistent"])
async def test_transfer_to_non_member_denied(pool, target):
    svc = TeamService(pool)
    await make_team(pool, 1)
    await make_team(pool, 2)
    await svc.create_team(TgUser(600, None, "Reg"), "RegOnly", "registrar")
    ids = {"outsider": 424242, "other_team": 21, "registrar": 600, "self": 10, "captain_of_other": 20, "nonexistent": 999999999}
    with pytest.raises((NotFound, Conflict)):
        await svc.transfer_captaincy(10, ids[target])
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams WHERE id = (SELECT team_id FROM team_members WHERE user_id=10)") == 10
    await assert_roster_valid(pool)


async def test_concurrent_transfers_exactly_one_wins(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    res = await asyncio.gather(*[svc.transfer_captaincy(10, 11 + (i % 4)) for i in range(20)], return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, NotCaptain) for r in res if isinstance(r, BaseException)), res
    await assert_roster_valid(pool)


async def test_transfer_racing_removal_of_the_target(pool):
    svc = TeamService(pool)
    for _ in range(5):
        # `tournaments` has FK columns pointing at `teams` (first/second/third_place_team_id), so
        # TRUNCATE teams ... CASCADE always wipes the active-tournament row too, regardless of
        # whether `tournaments` is named explicitly - it must be reseeded every iteration.
        await pool.execute("TRUNCATE broadcasts, matches, team_members, teams, solo_players, tournaments RESTART IDENTITY CASCADE")
        await pool.execute("INSERT INTO tournaments (status) VALUES ('active')")
        await make_team(pool, 1)
        res = await asyncio.gather(svc.transfer_captaincy(10, 11), svc.remove_member(10, 11), return_exceptions=True)
        assert all(not isinstance(r, BaseException) or isinstance(r, ServiceError) for r in res), res
        await assert_roster_valid(pool)


async def test_transfer_racing_a_join_at_full_capacity(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAPT, "Alpha", "captain")
    for i in range(3):
        await svc.join_team(player(i), v.invite_token)  # 4/5
    res = await asyncio.gather(
        svc.transfer_captaincy(CAPT.id, player(0).id),
        *[svc.join_team(TgUser(800 + i, None, f"J{i}"), v.invite_token) for i in range(10)], return_exceptions=True)
    assert all(not isinstance(r, BaseException) or isinstance(r, ServiceError) for r in res), res
    assert await db_members(pool) == 5
    await assert_roster_valid(pool)


# ---------------------------------------------------------------- 16: one active registration per user, one team per player
async def test_ordinary_user_can_register_only_one_team_admin_many(pool):
    svc = TeamService(pool)
    await svc.create_team(REG, "First", "registrar")
    for role in ("registrar", "member", "captain"):
        with pytest.raises(AlreadyRegistered):
            await svc.create_team(REG, f"Second {role}", role)
    for i in range(3):
        await svc.create_team(TgUser(1, "regist01", "Admin"), f"Admin team {i}", "registrar", is_admin=True)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams WHERE created_by = 1") == 4


async def test_registration_slot_frees_after_cancel(pool):
    svc = TeamService(pool)
    await svc.create_team(REG, "First", "member")
    assert await svc.cancel_registration(REG.id) == "First"
    assert await db_members(pool) == 0 and (await pool.fetchval("SELECT count(*) FROM teams")) == 0
    await svc.create_team(REG, "Second", "registrar")
    with pytest.raises(NotFound):
        await svc.cancel_registration(424242)


async def test_registrar_only_can_still_play_elsewhere_or_solo(pool):
    svc, reg = TeamService(pool), RegistrationService(pool)
    await svc.create_team(REG, "Mine", "registrar")
    other = await svc.create_team(CAPT, "Other", "captain")
    r = await svc.join_team(REG, other.invite_token)        # registrar-only is not a player yet
    assert not r.already_member and r.team.name == "Other"
    info = await reg.my_registration(REG.id)
    assert info.kind == "member" and info.team_role == "member" and info.plays
    assert (await svc.view_for_user(REG.id)).name == "Other"
    assert (await svc.registrar_view(REG.id)).name == "Mine"
    await svc.create_team(TgUser(70, None, "Reg2"), "Mine2", "registrar")
    await reg.register_solo(TgUser(70, None, "Reg2"), "EXP")
    info = await reg.my_registration(70)
    assert info.kind == "solo" and info.team_role == "registrar" and info.solo


async def test_no_duplicate_membership_across_roles(pool):
    svc = TeamService(pool)
    a = await svc.create_team(CAPT, "Team A", "captain")
    assert a.size == 1
    with pytest.raises(AlreadyRegistered):
        await svc.create_team(CAPT, "Team B", "captain")
    b = await svc.create_team(REG, "Team B", "registrar")
    with pytest.raises(AlreadyInOtherTeam):
        await svc.accept_captain(CAPT, b.captain_invite_token)   # already plays for A
    with pytest.raises(AlreadyInOtherTeam):
        await svc.join_team(CAPT, (await svc.create_team(NEWCAP, "Team C", "captain")).invite_token)
    assert await db_members(pool) == 2


async def test_solo_player_cannot_become_captain_or_be_member(pool):
    svc, reg = TeamService(pool), RegistrationService(pool)
    await reg.register_solo(TgUser(50, "solo0050", "Solo"), "GOLD")   # like the real 6323637322 GOLD record
    v = await svc.create_team(REG, "Alpha", "registrar")
    with pytest.raises(AlreadyRegistered):
        await svc.accept_captain(TgUser(50, "solo0050", "Solo"), v.captain_invite_token)
    with pytest.raises(AlreadyRegistered):
        await svc.create_team(TgUser(50, "solo0050", "Solo"), "Mine", "member")
    r = await svc.create_team(TgUser(50, "solo0050", "Solo"), "Mine", "registrar")   # registering is not playing
    assert r.size == 0
    assert (await pool.fetchval("SELECT role FROM solo_players WHERE user_id = 50")) == "GOLD"


async def test_registration_closed_blocks_new_captain_but_not_promotion(pool):
    svc, reg = TeamService(pool), RegistrationService(pool)
    v = await svc.create_team(REG, "Alpha", "registrar")
    w = await svc.create_team(TgUser(8, None, "R8"), "Beta", "member")
    await reg.set_open(False)
    with pytest.raises(RegistrationClosed):
        await svc.accept_captain(CAPT, v.captain_invite_token)
    assert (await svc.peek_captain_invite(CAPT.id, v.captain_invite_token)).status == "closed"
    assert (await svc.accept_captain(TgUser(8, None, "R8"), w.captain_invite_token)).was_member
    with pytest.raises(RegistrationClosed):
        await svc.create_team(TgUser(9, None, "R9"), "Gamma", "registrar")


# ---------------------------------------------------------------- 25: username NULL / display
async def test_no_username_in_any_role_and_display_is_clean(pool):
    svc = TeamService(pool)
    anon = lambda i, n: TgUser(i, None, n)  # noqa: E731
    v = await svc.create_team(anon(1, "Регистратор"), "Безымянные", "registrar")
    await svc.accept_captain(anon(2, "Капитан Без Ника"), v.captain_invite_token)
    await svc.join_team(anon(3, ""), v.invite_token)
    view = await svc.view_for_user(1)
    shown = [view.registrar_display] + [m.display for m in view.members]
    assert shown == ["Регистратор", "Капитан Без Ника", "Игрок 3"]
    assert not any(x in s for s in shown for x in ("None", "@", "null"))


async def test_users_with_username_show_name_and_at_username(pool):
    v = await TeamService(pool).create_team(TgUser(1, "capname1", "Cap Name"), "Alpha", "captain")
    assert v.captain.display == "Cap Name — @capname1"


# ---------------------------------------------------------------- DB-level guards
async def test_db_rejects_inconsistent_captain_state(pool):
    tid, ptok, ctok = await raw_pending_team(pool, 2)
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.PostgresError) as e:      # captain flag while the team has no captain
            await c.execute("UPDATE team_members SET is_captain = TRUE WHERE user_id = 9100")
        assert e.value.sqlstate == "MB002"
        with pytest.raises(asyncpg.PostgresError) as e:      # captain who is not a member
            await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (9999,'x')")
            await c.execute("UPDATE teams SET captain_id = 9999 WHERE id = $1", tid)
        assert e.value.sqlstate == "MB002"
        with pytest.raises(asyncpg.PostgresError) as e:      # a sixth member (one transaction, rolled back as a whole)
            async with c.transaction():
                for i in range(4):
                    await c.execute("INSERT INTO users (telegram_id, full_name) VALUES ($1,'x') ON CONFLICT DO NOTHING", 9200 + i)
                    await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1,$2)", tid, 9200 + i)
        assert e.value.sqlstate == "MB003"
    assert await db_members(pool, tid) == 2


async def test_admin_views_survive_captainless_teams(pool):
    svc = TeamService(pool)
    await svc.create_team(REG, "Pending R", "registrar")
    await svc.create_team(TgUser(2, None, "Pending M"), "Pending M", "member")
    adm = AdminService(pool)
    total, page, last, rows = await adm.teams_page(0)
    assert total == 2
    for r in rows:
        team, members = await adm.team_card(r["id"])
        assert team["created_by"] in (1, 2) and team["captain_id"] is None
    tid = rows[0]["id"]
    await adm.delete_team(tid)   # admin can remove a captain-less registration
    assert (await adm.teams_page(0))[0] == 1
