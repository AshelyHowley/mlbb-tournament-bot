"""Solo registration, registration switch and solo/team exclusivity (team mechanics: test_team_invites.py)."""
import asyncio

import pytest

from app.services.errors import AlreadyRegistered, RegistrationClosed, ServiceError, ValidationError
from app.services.registration import RegistrationService, TgUser
from app.services.team import TeamService
from tests.helpers import captain_of, uname

pytestmark = pytest.mark.db


async def test_solo_registration_and_view(pool):
    svc = RegistrationService(pool)
    reg = await svc.register_solo(TgUser(9, "SoloPlayer", "Solo P"), "JUNGLE")
    assert reg.kind == "solo"
    mine = await svc.my_registration(9)
    assert (mine.kind, mine.role, mine.username) == ("solo", "JUNGLE", "soloplayer")
    assert await svc.my_registration(10) is None
    with pytest.raises(AlreadyRegistered):
        await svc.register_solo(TgUser(9, "SoloPlayer"), "MID")


async def test_solo_without_username_is_allowed(pool):
    svc = RegistrationService(pool)
    reg = await svc.register_solo(TgUser(9, None, "No Name"), "EXP")
    assert reg.username is None and (await svc.my_registration(9)).kind == "solo"


@pytest.mark.parametrize("role", ["", "exp", "SUPPORT", "EXP; DROP TABLE users"])
async def test_solo_invalid_role(pool, role):
    with pytest.raises(ValidationError):
        await RegistrationService(pool).register_solo(TgUser(9, "soloplayer"), role)


async def test_registration_closed_blocks_new_but_keeps_existing(pool):
    svc, teams = RegistrationService(pool), TeamService(pool)
    await teams.create_team(captain_of(1), "Alpha")
    await svc.set_open(False)
    assert not await svc.is_open()
    with pytest.raises(RegistrationClosed):
        await teams.create_team(captain_of(2), "Beta")
    with pytest.raises(RegistrationClosed):
        await svc.register_solo(TgUser(9, "soloplayer"), "EXP")
    assert (await svc.my_registration(captain_of(1).id)).kind == "captain"
    await svc.set_open(True)
    await svc.register_solo(TgUser(9, "soloplayer"), "EXP")


async def test_my_registration_kinds(pool):
    svc, teams = RegistrationService(pool), TeamService(pool)
    v = await teams.create_team(captain_of(1), "Alpha")
    await teams.join_team(TgUser(5, None, "Member"), v.invite_token)
    assert (await svc.my_registration(captain_of(1).id)).kind == "captain"
    assert (await svc.my_registration(5)).kind == "member"


async def test_concurrent_solo_double_submit(pool):
    svc = RegistrationService(pool)
    res = await asyncio.gather(*[svc.register_solo(TgUser(9, "soloplayer"), "EXP") for _ in range(20)], return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, ServiceError) for r in res if isinstance(r, BaseException)), res


async def test_concurrent_solo_vs_join_never_both(pool):
    """The same person racing solo registration against joining a team ends up in exactly one place."""
    teams, svc = TeamService(pool), RegistrationService(pool)
    for i in range(1, 11):
        v = await teams.create_team(captain_of(i), f"Race {i}")
        uid = 5000 + i
        res = await asyncio.gather(
            svc.register_solo(TgUser(uid, uname(i, "racer"), "R"), "EXP"),
            teams.join_team(TgUser(uid, uname(i, "racer"), "R"), v.invite_token),
            return_exceptions=True,
        )
        assert sum(not isinstance(r, BaseException) for r in res) == 1, res
        assert all(isinstance(r, ServiceError) for r in res if isinstance(r, BaseException)), res
        async with pool.acquire() as c:
            in_team = await c.fetchval("SELECT count(*) FROM team_members WHERE user_id = $1", uid)
            solo = await c.fetchval("SELECT count(*) FROM solo_players WHERE user_id = $1", uid)
        assert in_team + solo == 1
