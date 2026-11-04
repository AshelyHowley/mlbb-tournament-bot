"""Invite-link team mechanics: create -> join by secret token -> manage roster. Identity = Telegram user_id."""
import asyncio

# import asyncpg  # removed for SQLite
import pytest

from app.services.errors import (
    AlreadyInOtherTeam,
    AlreadyRegistered,
    Conflict,
    InvalidInvite,
    NotCaptain,
    NotFound,
    RegistrationClosed,
    ServiceError,
    TeamFull,
    TeamNameTaken,
    ValidationError,
)
from app.services.registration import RegistrationService, TgUser
from app.services.team import MAX_TEAM_SIZE, TeamService
from app.utils.text import INVITE_TOKEN_RE, parse_invite_payload
from tests.helpers import captain_of, make_team, member_of, team_token

pytestmark = pytest.mark.db

CAP = captain_of(1)


async def sizes(pool):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT count(*) FROM team_members"), await c.fetchval("SELECT count(*) FROM teams")


# 1-3 ------------------------------------------------------------------------------------------
async def test_create_team_makes_captain_member_number_one(pool):
    svc = TeamService(pool)
    view = await svc.create_team(CAP, "  Alpha   Team ")
    assert view.name == "Alpha Team" and view.size == 1 and view.free == 4
    assert view.captain.user_id == CAP.id and view.members[0].is_captain
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT is_captain, user_id FROM team_members")
        assert (row["is_captain"], row["user_id"]) == (True, CAP.id)
        assert await c.fetchval("SELECT captain_id FROM teams") == CAP.id


async def test_invite_token_is_secret_random_and_unique(pool):
    svc = TeamService(pool)
    tokens = set()
    for i in range(1, 8):
        v = await svc.create_team(captain_of(i), f"Team {i}")
        assert INVITE_TOKEN_RE.fullmatch(v.invite_token) and len(v.invite_token) >= 32
        tokens.add(v.invite_token)
    assert len(tokens) == 7
    payload = f"team_{v.invite_token}"
    assert len(payload) <= 64 and parse_invite_payload(payload) == v.invite_token  # fits Telegram's start payload
    async with pool.acquire() as c:  # DB-level uniqueness
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("UPDATE teams SET invite_token = $1 WHERE id = 1", v.invite_token)


# 4-8 ------------------------------------------------------------------------------------------
async def test_members_join_up_to_five_and_sixth_is_refused(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    for k in range(1, 5):  # members #2..#5
        r = await svc.join_team(member_of(1, k), v.invite_token)
        assert not r.already_member and r.team.size == k + 1
    assert r.team.size == MAX_TEAM_SIZE and r.team.free == 0
    with pytest.raises(TeamFull) as e:
        await svc.join_team(member_of(1, 5), v.invite_token)  # member #6
    assert "нет свободных мест" in e.value.user_message
    assert (await sizes(pool))[0] == 5


async def test_two_simultaneous_joins_for_the_last_seat(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    for k in range(1, 4):
        await svc.join_team(member_of(1, k), v.invite_token)  # 4/5
    res = await asyncio.gather(svc.join_team(member_of(1, 4), v.invite_token),
                               svc.join_team(member_of(1, 5), v.invite_token), return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert sum(isinstance(r, TeamFull) for r in res) == 1
    assert (await sizes(pool))[0] == 5


@pytest.mark.parametrize("n", [10, 20, 50])
async def test_many_simultaneous_joins_never_exceed_five(pool, n):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    for k in range(1, 4):
        await svc.join_team(member_of(1, k), v.invite_token)  # 4/5
    res = await asyncio.gather(*[svc.join_team(TgUser(1000 + i, f"racer{i:03d}", f"R{i}"), v.invite_token) for i in range(n)],
                               return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, TeamFull) for r in res if isinstance(r, BaseException)), res
    assert (await sizes(pool))[0] == 5


async def test_concurrent_joins_from_empty_team_cap_at_five(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    res = await asyncio.gather(*[svc.join_team(TgUser(2000 + i, None, f"P{i}"), v.invite_token) for i in range(50)],
                               return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 4  # captain + 4
    assert all(isinstance(r, TeamFull) for r in res if isinstance(r, BaseException))
    assert (await sizes(pool))[0] == 5


# 10-11 ----------------------------------------------------------------------------------------
async def test_repeated_click_of_same_player_is_idempotent(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    first = await svc.join_team(member_of(1, 1), v.invite_token)
    again = await svc.join_team(member_of(1, 1), v.invite_token)
    assert not first.already_member and again.already_member and again.team.size == 2
    res = await asyncio.gather(*[svc.join_team(member_of(1, 1), v.invite_token) for _ in range(20)], return_exceptions=True)
    assert all(not isinstance(r, BaseException) and r.already_member for r in res)
    assert (await sizes(pool))[0] == 2


async def test_captain_opening_own_link_is_already_member(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    assert (await svc.join_team(CAP, v.invite_token)).already_member
    assert (await svc.peek_invite(CAP.id, v.invite_token)).status == "mine"


async def test_concurrent_double_join_same_player_single_row(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    res = await asyncio.gather(*[svc.join_team(member_of(1, 1), v.invite_token) for _ in range(20)])
    assert sum(not r.already_member for r in res) == 1
    assert (await sizes(pool))[0] == 2


# 11 ---------------------------------------------------------------------------------------------
async def test_player_already_in_other_team(pool):
    svc = TeamService(pool)
    a = await svc.create_team(captain_of(1), "Alpha")
    b = await svc.create_team(captain_of(2), "Beta")
    await svc.join_team(member_of(1, 1), a.invite_token)
    with pytest.raises(AlreadyInOtherTeam) as e:
        await svc.join_team(member_of(1, 1), b.invite_token)
    assert "уже состоите в другой команде" in e.value.user_message
    with pytest.raises(AlreadyInOtherTeam):  # a captain cannot join another team either
        await svc.join_team(captain_of(2), a.invite_token)
    assert (await svc.peek_invite(member_of(1, 1).id, b.invite_token)).status == "other"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE user_id = $1", member_of(1, 1).id) == 1


async def test_concurrent_join_two_teams_only_one_wins(pool):
    svc = TeamService(pool)
    a = await svc.create_team(captain_of(1), "Alpha")
    b = await svc.create_team(captain_of(2), "Beta")
    res = await asyncio.gather(*[svc.join_team(TgUser(777, None, "Same"), (a if i % 2 else b).invite_token) for i in range(20)],
                               return_exceptions=True)
    wins = [r for r in res if not isinstance(r, BaseException) and not r.already_member]
    assert len(wins) == 1
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE user_id = 777") == 1


# 12 ---------------------------------------------------------------------------------------------
async def test_username_null_everywhere(pool):
    svc = TeamService(pool)
    cap = TgUser(1, None, "Капитан Без Username")
    v = await svc.create_team(cap, "NoNames")
    await svc.join_team(TgUser(2, None, "Игрок Два"), v.invite_token)
    r2 = await svc.join_team(TgUser(3, None, ""), v.invite_token)  # no username AND no name
    shown = [m.display for m in r2.team.members]
    assert shown[0] == "Капитан Без Username" and shown[1] == "Игрок Два" and shown[2] == "Игрок 3"
    assert not any("None" in s or "@" in s for s in shown)
    solo = await RegistrationService(pool).register_solo(TgUser(9, None, "Solo NoName"), "EXP")
    assert solo.kind == "solo" and solo.username is None
    assert (await RegistrationService(pool).my_registration(9)).kind == "solo"
    assert (await RegistrationService(pool).my_registration(2)).kind == "member"


async def test_username_shown_when_present(pool):
    svc = TeamService(pool)
    v = await svc.create_team(TgUser(1, "capname1", "Cap Name"), "Alpha")
    assert v.captain.display == "Cap Name — @capname1"
    assert (await svc.view_for_user(1)).captain.display == "Cap Name — @capname1"


async def test_changed_username_does_not_break_membership(pool):
    svc = TeamService(pool)
    v = await svc.create_team(TgUser(1, "oldname1", "Cap"), "Alpha")
    await svc.join_team(TgUser(2, "player02", "P2"), v.invite_token)
    async with pool.acquire() as c:  # Telegram user renames / removes username
        await c.execute("UPDATE users SET username = NULL, username_norm = NULL WHERE telegram_id = 2")
    view = await svc.view_for_user(2)
    assert view is not None and view.size == 2 and view.members[1].display == "P2"


# 13-15 ----------------------------------------------------------------------------------------
async def test_remove_member_frees_the_seat_and_new_player_can_join(pool):
    svc = TeamService(pool)
    tid = await make_team(pool, 1)
    token = await team_token(pool, tid)
    with pytest.raises(TeamFull):
        await svc.join_team(TgUser(500, None, "New"), token)
    removed, view = await svc.remove_member(CAP.id, member_of(1, 2).id)
    assert removed.user_id == member_of(1, 2).id and view.size == 4 and view.free == 1
    assert all(m.user_id != member_of(1, 2).id for m in view.members)
    assert await svc.view_for_user(member_of(1, 2).id) is None
    r = await svc.join_team(TgUser(500, None, "New"), token)
    assert r.team.size == 5
    with pytest.raises(TeamFull):
        await svc.join_team(TgUser(501, None, "Extra"), token)


async def test_removed_player_can_join_another_team(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    b = await svc.create_team(captain_of(2), "Beta")
    await svc.remove_member(CAP.id, member_of(1, 1).id)
    r = await svc.join_team(member_of(1, 1), b.invite_token)
    assert r.team.name == "Beta" and r.team.size == 2


# 16 ---------------------------------------------------------------------------------------------
async def test_replacement_flow_remove_then_new_player_by_same_link(pool):
    svc = TeamService(pool)
    tid = await make_team(pool, 1)
    token = await team_token(pool, tid)
    await svc.remove_member(CAP.id, member_of(1, 3).id)
    newcomer = TgUser(900, "newbie01", "Newbie")  # username never known in advance
    r = await svc.join_team(newcomer, token)
    assert r.team.size == 5 and {m.user_id for m in r.team.members} >= {CAP.id, 900}
    assert (await svc.view_for_user(member_of(1, 3).id)) is None
    assert await team_token(pool, tid) == token  # same link keeps working


# 17-18 ------------------------------------------------------------------------------------------
async def test_captain_cannot_be_removed_or_leave(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    with pytest.raises(Conflict) as e:
        await svc.remove_member(CAP.id, CAP.id)
    assert "Капитана нельзя" in e.value.user_message
    with pytest.raises(Conflict) as e:
        await svc.leave_team(CAP.id)
    assert "администратору" in e.value.user_message
    assert (await svc.view_for_user(CAP.id)).size == 5


async def test_captain_row_cannot_be_deleted_even_with_raw_sql(pool):
    await make_team(pool, 1)
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("DELETE FROM team_members WHERE is_captain")
        assert e.value.sqlstate == "MB002"
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5


async def test_regular_player_cannot_remove_others(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    with pytest.raises(NotCaptain):
        await svc.remove_member(member_of(1, 1).id, member_of(1, 2).id)
    with pytest.raises(NotCaptain):
        await svc.remove_member(member_of(1, 1).id, CAP.id)
    with pytest.raises(NotCaptain):
        await svc.remove_member(424242, member_of(1, 2).id)  # outsider
    assert (await svc.view_for_user(CAP.id)).size == 5


async def test_captain_cannot_remove_players_of_another_team(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    await make_team(pool, 2)
    with pytest.raises(NotFound):
        await svc.remove_member(captain_of(1).id, member_of(2, 1).id)  # forged target from a foreign team
    assert (await svc.view_for_user(captain_of(2).id)).size == 5


async def test_leave_team_frees_seat(pool):
    svc = TeamService(pool)
    tid = await make_team(pool, 1)
    me, view = await svc.leave_team(member_of(1, 4).id)
    assert view.size == 4 and me.user_id == member_of(1, 4).id
    with pytest.raises(NotFound):
        await svc.leave_team(member_of(1, 4).id)  # already out
    await svc.join_team(member_of(1, 4), await team_token(pool, tid))  # can come back


# 20-22 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "short", "x" * 31, "x" * 49, "a" * 32 + "\n", "a b" * 12, "../" * 12,
                                 "a'; DROP TABLE teams;--" + "a" * 20, "б" * 32, None])
async def test_forged_or_malformed_token(pool, bad):
    svc = TeamService(pool)
    await make_team(pool, 1)
    with pytest.raises(InvalidInvite):
        await svc.join_team(TgUser(600, None, "X"), bad)
    assert (await svc.peek_invite(600, bad)).status == "invalid"
    assert (await sizes(pool))[0] == 5


async def test_nonexistent_well_formed_token(pool):
    svc = TeamService(pool)
    await make_team(pool, 1)
    with pytest.raises(InvalidInvite):
        await svc.join_team(TgUser(600, None, "X"), "A" * 32)
    assert (await svc.peek_invite(600, "A" * 32)).status == "invalid"


async def test_token_of_deleted_team_is_dead(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    async with pool.acquire() as c:
        await c.execute("DELETE FROM teams WHERE id = $1", v.team_id)
        assert await c.fetchval("SELECT count(*) FROM team_members") == 0  # cascade
    with pytest.raises(InvalidInvite):
        await svc.join_team(TgUser(600, None, "X"), v.invite_token)
    assert await svc.view_for_user(CAP.id) is None
    await svc.create_team(CAP, "Alpha Again")  # captain is free again


async def test_sequential_team_id_is_not_an_invite(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    for guess in (str(v.team_id), str(v.team_id).zfill(32), "team_" + str(v.team_id)):
        with pytest.raises(InvalidInvite):
            await svc.join_team(TgUser(600, None, "X"), guess)


# registration switch / solo interplay -----------------------------------------------------------
async def test_registration_closed_blocks_new_joins_but_not_existing_members(pool):
    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    await svc.join_team(member_of(1, 1), v.invite_token)
    await RegistrationService(pool).set_open(False)
    with pytest.raises(RegistrationClosed):
        await svc.join_team(member_of(1, 2), v.invite_token)
    assert (await svc.join_team(member_of(1, 1), v.invite_token)).already_member
    with pytest.raises(RegistrationClosed):
        await svc.create_team(captain_of(2), "Beta")
    assert (await svc.peek_invite(member_of(1, 2).id, v.invite_token)).status == "closed"


async def test_solo_and_team_membership_are_mutually_exclusive(pool):
    svc, reg = TeamService(pool), RegistrationService(pool)
    v = await svc.create_team(CAP, "Alpha")
    await reg.register_solo(TgUser(50, "solo0050", "Solo"), "MID")
    with pytest.raises(AlreadyRegistered):
        await svc.join_team(TgUser(50, "solo0050", "Solo"), v.invite_token)
    with pytest.raises(AlreadyRegistered):
        await reg.register_solo(CAP, "MID")  # captain cannot go solo
    await svc.join_team(member_of(1, 1), v.invite_token)
    with pytest.raises(AlreadyRegistered):
        await reg.register_solo(member_of(1, 1), "EXP")
    with pytest.raises(AlreadyRegistered):
        await svc.create_team(TgUser(50, "solo0050", "Solo"), "Gamma")


async def test_raw_sql_cannot_mix_solo_and_team(pool):
    v = await TeamService(pool).create_team(CAP, "Alpha")
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO solo_players (user_id, role) VALUES ($1, 'EXP')", CAP.id)
        assert e.value.sqlstate == "MB001"
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (60, 'S')")
        await c.execute("INSERT INTO solo_players (user_id, role) VALUES (60, 'EXP')")
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 60)", v.team_id)
        assert e.value.sqlstate == "MB001"


# create-team validation --------------------------------------------------------------------------
async def test_create_team_validation_and_duplicates(pool):
    svc = TeamService(pool)
    with pytest.raises(ValidationError):
        await svc.create_team(CAP, "A")
    await svc.create_team(CAP, "Alpha")
    with pytest.raises(AlreadyRegistered):
        await svc.create_team(CAP, "Beta")  # one team per captain
    for dup in ("ALPHA", "alpha", "  Alpha  "):
        with pytest.raises(TeamNameTaken):
            await svc.create_team(captain_of(2), dup)
    assert (await sizes(pool)) == (1, 1)


async def test_concurrent_double_create_yields_one_team(pool):
    svc = TeamService(pool)
    res = await asyncio.gather(*[svc.create_team(CAP, "Alpha") for _ in range(20)], return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, ServiceError) for r in res if isinstance(r, BaseException)), res
    assert await sizes(pool) == (1, 1)


async def test_concurrent_same_name_different_captains(pool):
    svc = TeamService(pool)
    res = await asyncio.gather(*[svc.create_team(captain_of(i), "Same Name") for i in range(1, 16)], return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, TeamNameTaken) for r in res if isinstance(r, BaseException))


# 26: rollback ------------------------------------------------------------------------------------
async def test_failed_create_rolls_back_everything(pool, monkeypatch):
    from app.repositories import teams as teams_repo

    async def boom(*a, **k):
        raise RuntimeError("simulated failure after the team row was inserted")

    monkeypatch.setattr(teams_repo, "add_member", boom)
    with pytest.raises(RuntimeError):
        await TeamService(pool).create_team(CAP, "Alpha")
    monkeypatch.undo()
    assert await sizes(pool) == (0, 0)  # no orphan team without a captain member
    await TeamService(pool).create_team(CAP, "Alpha")  # and the captain is not stuck


async def test_failed_join_rolls_back_and_leaves_roster_unchanged(pool, monkeypatch):
    from app.repositories import teams as teams_repo

    svc = TeamService(pool)
    v = await svc.create_team(CAP, "Alpha")
    real = teams_repo.add_member

    async def add_then_fail(conn, *a, **k):
        await real(conn, *a, **k)
        raise RuntimeError("late failure inside the transaction")

    monkeypatch.setattr(teams_repo, "add_member", add_then_fail)
    with pytest.raises(RuntimeError):
        await svc.join_team(member_of(1, 1), v.invite_token)
    monkeypatch.undo()
    assert (await sizes(pool))[0] == 1
    assert (await svc.join_team(member_of(1, 1), v.invite_token)).team.size == 2


async def test_db_itself_rejects_sixth_member_and_membership_moves(pool):
    tid = await make_team(pool, 1)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (700, 'Six')")
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 700)", tid)
        assert e.value.sqlstate == "MB003"
        with pytest.raises(asyncpg.PostgresError) as e:  # cannot smuggle a member in by re-pointing a row
            await c.execute("UPDATE team_members SET user_id = 700 WHERE user_id = $1", member_of(1, 1).id)
        assert e.value.sqlstate == "MB004"
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5
