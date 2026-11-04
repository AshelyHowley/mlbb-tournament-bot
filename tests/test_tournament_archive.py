"""Tournament finalize/archive: 1st/2nd/3rd placement rules, early close, atomic archive, immutable
history snapshot, and a brand-new empty active tournament afterwards."""
import asyncio

import pytest

from app.services.admin import AdminService
from app.services.errors import NotFound, TournamentNotReady
from app.services.registration import RegistrationService, TgUser
from app.services.series import SeriesService
from app.services.team import TeamService
from app.services.tournament import TournamentService
from tests.helpers import add_user, make_team

pytestmark = pytest.mark.db


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


ADMIN = TgUser(999, "theadmin", "Admin")


async def _bracket_to_final(pool, series: SeriesService, adm: AdminService):
    """4 teams -> 2 semis -> third place + final, everything decided. Returns (champion, second, third)."""
    a, b, c, d = [await make_team(pool, i, f"Team {chr(64 + i)}") for i in range(1, 5)]
    s1 = await adm.create_match(a, b, "semi")
    s2 = await adm.create_match(c, d, "semi")
    for _ in range(3):
        await series.add_game(ADMIN, s1, a)  # a beats b
    for _ in range(3):
        await series.add_game(ADMIN, s2, d)  # d beats c
    third = await adm.create_match(b, c, "third")
    final = await adm.create_match(a, d, "final")
    for _ in range(3):
        await series.add_game(ADMIN, third, b)
    for _ in range(3):
        await series.add_game(ADMIN, final, a)
    return a, d, b  # champion=a, second=d, third=b


async def test_placements_not_ready_before_final_and_third(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    a, b = await make_team(pool, 1, "AA"), await make_team(pool, 2, "BB")
    p = await tour.placements()
    assert not p.ready and p.first_id is None
    mid = await adm.create_match(a, b, "final")
    for _ in range(3):
        await series.add_game(ADMIN, mid, a)
    p = await tour.placements()
    assert not p.ready  # final done but no third-place match at all
    assert p.first_id == a and p.second_id == b and p.third_id is None


async def test_full_bracket_placements_correct(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    champ, second, third = await _bracket_to_final(pool, series, adm)
    p = await tour.placements()
    assert p.ready and p.first_id == champ and p.second_id == second and p.third_id == third
    assert p.first_id != p.second_id != p.third_id != p.first_id  # three distinct teams


async def test_finalize_requires_force_when_not_ready(pool):
    tour = TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    with pytest.raises(TournamentNotReady):
        await tour.finalize(ADMIN, force=False)
    tid, p = await tour.finalize(ADMIN, force=True)  # early close allowed with explicit force
    assert not p.ready
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM tournaments WHERE id = $1", tid) == "closed_early"


async def test_finalize_normal_completion(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    champ, second, third = await _bracket_to_final(pool, series, adm)
    old_active = await tour.active_id()
    tid, p = await tour.finalize(ADMIN, force=False)
    assert tid == old_active and p.first_id == champ
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT status, completed_by, first_place_team_id, second_place_team_id, third_place_team_id FROM tournaments WHERE id=$1", tid)
    assert row["status"] == "completed" and row["completed_by"] == ADMIN.id
    assert (row["first_place_team_id"], row["second_place_team_id"], row["third_place_team_id"]) == (champ, second, third)


async def test_new_active_tournament_created_and_is_empty(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    old_active = await tour.active_id()
    await _bracket_to_final(pool, series, adm)
    await tour.finalize(ADMIN, force=False)
    new_active = await tour.active_id()
    assert new_active != old_active
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams WHERE tournament_id = $1", new_active) == 0
        assert await c.fetchval("SELECT count(*) FROM solo_players WHERE tournament_id = $1", new_active) == 0
        assert await c.fetchval("SELECT count(*) FROM matches WHERE tournament_id = $1", new_active) == 0
        assert await c.fetchval("SELECT status FROM tournaments WHERE id = $1", old_active) == "completed"


async def test_only_one_active_tournament_ever(pool):
    tour = TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    await tour.finalize(ADMIN, force=True)
    await tour.finalize(ADMIN, force=True)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status = 'active'") == 1


async def test_concurrent_finalize_exactly_one_archive_one_new_active(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    await _bracket_to_final(pool, series, adm)
    await add_user(pool, ADMIN.id, ADMIN.username)
    deputy = TgUser(1000, "deputy001", "Deputy")
    await add_user(pool, deputy.id, deputy.username)
    res = await asyncio.gather(tour.finalize(ADMIN, force=False), tour.finalize(deputy, force=False), return_exceptions=True)
    ok = [r for r in res if not isinstance(r, BaseException)]
    assert len(ok) == 1  # the second finalize finds no active tournament left to finalize
    assert all(isinstance(r, (NotFound,)) for r in res if isinstance(r, BaseException))
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status = 'active'") == 1
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status != 'active'") == 1


async def test_registration_scoped_to_new_tournament_old_player_can_register_again(pool):
    teams, tour = TeamService(pool), TournamentService(pool)
    await teams.create_team(TgUser(10, "cap0010", "Cap"), "Old Team", "captain")
    await add_user(pool, ADMIN.id, ADMIN.username)
    await tour.finalize(ADMIN, force=True)
    # the same Telegram user_id can captain a brand-new team in the new tournament
    v = await teams.create_team(TgUser(10, "cap0010", "Cap"), "Old Team", "captain")  # even reuse the SAME name
    assert v.name == "Old Team" and v.size == 1


async def test_solo_registration_scoped_to_new_tournament(pool):
    reg, tour = RegistrationService(pool), TournamentService(pool)
    await reg.register_solo(TgUser(50, "solo0050", "Solo"), "GOLD")
    await add_user(pool, ADMIN.id, ADMIN.username)
    await tour.finalize(ADMIN, force=True)
    reg2 = await reg.register_solo(TgUser(50, "solo0050", "Solo"), "MID")  # free again in the new tournament
    assert reg2.role == "MID"


async def test_general_broadcast_only_reaches_active_tournament_participants(pool):
    from app.repositories import broadcasts as bc_repo

    teams, tour = TeamService(pool), TournamentService(pool)
    await teams.create_team(TgUser(10, "cap0010", "Cap"), "Old Team", "captain")
    await add_user(pool, ADMIN.id, ADMIN.username)
    await tour.finalize(ADMIN, force=True)
    await teams.create_team(TgUser(20, "cap0020", "Cap2"), "New Team", "captain")
    async with pool.acquire() as c:
        recipients = await bc_repo.recipients(c, "captains")
    assert recipients == [20]  # not the archived tournament's captain (10)


# ---------------------------------------------------------------- history
async def test_history_lists_completed_tournaments_newest_first(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    await _bracket_to_final(pool, series, adm)
    tid1, _ = await tour.finalize(ADMIN, force=False)
    await _bracket_to_final(pool, series, adm)
    tid2, _ = await tour.finalize(ADMIN, force=False)
    total, page, last, rows = await tour.history_page(0)
    assert total == 2 and [r["id"] for r in rows] == [tid2, tid1]


async def test_history_is_read_only_no_write_methods_exposed(pool):
    tour = TournamentService(pool)
    write_like = [m for m in dir(tour) if any(k in m for k in ("edit", "delete", "update")) and not m.startswith("_")]
    assert write_like == []


async def test_history_snapshot_survives_current_team_changes(pool):
    """A team's roster/name in the ARCHIVED tournament must not shift when new, unrelated data changes."""
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    champ, second, third = await _bracket_to_final(pool, series, adm)
    tid, p = await tour.finalize(ADMIN, force=False)
    teams_before = {t["id"]: t["name"] for t in await tour.teams(tid)}
    # create unrelated new-tournament data
    await make_team(pool, 99, "Unrelated New Team")
    teams_after = {t["id"]: t["name"] for t in await tour.teams(tid)}
    assert teams_before == teams_after and 99 * 10 not in [m for m in teams_after]


async def test_history_username_change_does_not_alter_display_snapshot(pool):
    """Changing a player's live Telegram username after archiving must not change history's roster text."""
    adm, series, teams, tour = AdminService(pool), SeriesService(pool, FakeBot()), TeamService(pool), TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    v = await teams.create_team(TgUser(10, "oldname01", "Cap"), "Solo Team", "captain")
    await teams.join_team(TgUser(11, "member011", "Mem"), v.invite_token)
    other = await make_team(pool, 2, "Other")
    mid = await adm.create_match(v.team_id, other, "final")
    for _ in range(3):
        await series.add_game(ADMIN, mid, v.team_id)
    tid, p = await tour.finalize(ADMIN, force=True)
    _, members, _ = await tour.team_card(v.team_id)
    before = {m["user_id"]: (m["username"], m["full_name"]) for m in members}
    assert before[10] == ("oldname01", "Cap") and before[11] == ("member011", "Mem")
    async with pool.acquire() as c:  # live username/name change AFTER archive
        await c.execute(
            "UPDATE users SET username='brandnewname', username_norm='brandnewname', full_name='New Name' WHERE telegram_id=10"
        )
    _, members, _ = await tour.team_card(v.team_id)
    after = {m["user_id"]: (m["username"], m["full_name"]) for m in members}
    assert after == before  # frozen: the live change is invisible in this archived tournament's history
    # meanwhile the ACTIVE tournament's own display of the SAME user_id (if any) stays live - not
    # applicable here since user 10 has no active-tournament registration after the archive, but the
    # repository query itself (teams.members) always prefers the live row when no snapshot exists yet,
    # which is exercised by every other (non-archived) team_card test in this suite.


async def test_history_stats_are_real_and_computed(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    await _bracket_to_final(pool, series, adm)
    tid, p = await tour.finalize(ADMIN, force=False)
    row, stats = await tour.detail(tid)
    assert stats["teams"] == 4 and stats["series"] == 4 and stats["games"] == 12  # 4 series x 3 games each
    breakdown = await tour.bo5_breakdown(tid)
    assert breakdown and all(b["win"] == 3 and b["lose"] == 0 for b in breakdown)


async def test_history_team_card_shows_placement_and_real_roster_size(pool):
    adm, series, tour = AdminService(pool), SeriesService(pool, FakeBot()), TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    champ, second, third = await _bracket_to_final(pool, series, adm)
    tid, p = await tour.finalize(ADMIN, force=False)
    team, members, played = await tour.team_card(champ)
    assert len(members) == 5 and len(played) == 2  # semi + final
    wins = sum(1 for m in played if m["winner_id"] == champ)
    assert wins == 2


async def test_early_close_does_not_invent_placements(pool):
    tour = TournamentService(pool)
    await add_user(pool, ADMIN.id, ADMIN.username)
    await make_team(pool, 1, "OnlyTeam")
    tid, p = await tour.finalize(ADMIN, force=True)
    assert p.first_id is None and p.second_id is None and p.third_id is None
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT first_place_team_id, second_place_team_id, third_place_team_id FROM tournaments WHERE id=$1", tid)
    assert row["first_place_team_id"] is None and row["second_place_team_id"] is None and row["third_place_team_id"] is None
