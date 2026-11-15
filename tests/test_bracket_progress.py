"""br:win logic: advancing winners, correcting a result, auto 3rd-place match, bracket image."""
import pytest

from app.services.bracket_builder import plan_bracket, save_plan
from app.services.bracket_image import render_bracket_image
from app.services.bracket_progress import record_winner
from app.services.errors import Conflict, ValidationError
from tests.helpers import make_team, set_winner

pytestmark = pytest.mark.db


async def _bracket(pool, n):
    teams = [await make_team(pool, i, f"Team {i}") for i in range(1, n + 1)]
    async with pool.acquire() as c, c.transaction():
        tid = await c.fetchval("SELECT id FROM tournaments WHERE status = 'active'")
        await save_plan(c, tid, plan_bracket(teams, seed=1))
    return teams


async def _rows(pool, stage):
    async with pool.acquire() as c:
        return await c.fetch("SELECT * FROM matches WHERE stage_code = $1 ORDER BY id", stage)


async def test_four_teams_auto_third_place_and_podium(pool):
    await _bracket(pool, 4)
    s1, s2 = await _rows(pool, "semi")
    await set_winner(pool, s1["id"], s1["team_a_id"])
    assert await _rows(pool, "third") == []  # only after BOTH semis
    await set_winner(pool, s2["id"], s2["team_b_id"])
    (third,) = await _rows(pool, "third")
    assert {third["team_a_id"], third["team_b_id"]} == {s1["team_b_id"], s2["team_a_id"]}
    (final,) = await _rows(pool, "final")
    assert {final["team_a_id"], final["team_b_id"]} == {s1["team_a_id"], s2["team_b_id"]}


async def test_correcting_a_result_replaces_the_advanced_team(pool):
    await _bracket(pool, 4)
    s1, s2 = await _rows(pool, "semi")
    await set_winner(pool, s1["id"], s1["team_a_id"])
    await set_winner(pool, s2["id"], s2["team_a_id"])
    await set_winner(pool, s1["id"], s1["team_b_id"])  # admin clicked the wrong team first
    (final,) = await _rows(pool, "final")
    assert {final["team_a_id"], final["team_b_id"]} == {s1["team_b_id"], s2["team_a_id"]}
    (third,) = await _rows(pool, "third")
    assert {third["team_a_id"], third["team_b_id"]} == {s1["team_a_id"], s2["team_b_id"]}


async def test_cannot_change_result_after_next_match_played(pool):
    await _bracket(pool, 4)
    s1, s2 = await _rows(pool, "semi")
    await set_winner(pool, s1["id"], s1["team_a_id"])
    await set_winner(pool, s2["id"], s2["team_a_id"])
    (final,) = await _rows(pool, "final")
    await set_winner(pool, final["id"], final["team_a_id"])
    with pytest.raises(Conflict):
        await set_winner(pool, s1["id"], s1["team_b_id"])
    (final2,) = await _rows(pool, "final")
    assert final2["winner_id"] == final["team_a_id"]  # rolled back, nothing changed


async def test_winner_must_be_a_participant_and_pair_complete(pool):
    teams = await _bracket(pool, 4)
    s1, _ = await _rows(pool, "semi")
    outsider = next(t for t in teams if t not in (s1["team_a_id"], s1["team_b_id"]))
    with pytest.raises(ValidationError):
        await set_winner(pool, s1["id"], outsider)
    (final,) = await _rows(pool, "final")
    with pytest.raises(ValidationError):  # final has no teams yet
        await set_winner(pool, final["id"], s1["team_a_id"])


async def test_same_winner_twice_is_a_noop(pool):
    await _bracket(pool, 4)
    s1, _ = await _rows(pool, "semi")
    await set_winner(pool, s1["id"], s1["team_a_id"])
    async with pool.acquire() as c, c.transaction():
        assert await record_winner(c, s1["id"], s1["team_a_id"]) is False


async def test_champion_box_filled_and_32_plus_team_bracket_saves(pool):
    await _bracket(pool, 40)  # needs the r32 stage (migration 020)
    assert len(await _rows(pool, "r32")) > 0


async def test_bracket_image_draws_third_place(pool):
    await _bracket(pool, 4)
    s1, s2 = await _rows(pool, "semi")
    await set_winner(pool, s1["id"], s1["team_a_id"])
    await set_winner(pool, s2["id"], s2["team_a_id"])
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT m.id, m.stage_code AS stage, m.next_match_id, ta.name AS team_a, tb.name AS team_b, tw.name AS winner "
            "FROM matches m LEFT JOIN teams ta ON ta.id = m.team_a_id LEFT JOIN teams tb ON tb.id = m.team_b_id "
            "LEFT JOIN teams tw ON tw.id = m.winner_id ORDER BY m.id"
        )
    with_third = render_bracket_image([dict(r) for r in rows])
    without = render_bracket_image([dict(r) for r in rows if r["stage"] != "third"])
    assert with_third[:8] == b"\x89PNG\r\n\x1a\n"
    from io import BytesIO
    from PIL import Image
    assert Image.open(BytesIO(with_third)).height > Image.open(BytesIO(without)).height


# ------------------------------------------------------------------ admin buttons
import pytest_asyncio  # noqa: E402

from tests.harness import ADMIN_ID, Harness  # noqa: E402


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]  # no handler may crash
    hh.close()


async def _count(pool, sql):
    async with pool.acquire() as c:
        return await c.fetchval(sql)


async def test_test_team_buttons_add_playable_teams(h, pool):
    await h.press(ADMIN_ID, "adm", "adm:tadd16")
    assert "Добавлено 16" in h.last()
    # every test team has a captain on the roster, so it shows up in manual pairing too
    assert await _count(pool, "SELECT count(*) FROM teams t WHERE EXISTS "
                              "(SELECT 1 FROM team_members m WHERE m.team_id = t.id AND m.is_captain = 1)") == 16
    await h.press(ADMIN_ID, "adm", "adm:taddrnd")
    assert await _count(pool, "SELECT count(*) FROM teams") > 16


async def test_random_bracket_delete_pair_and_clear(h, pool):
    await h.press(ADMIN_ID, "adm", "adm:tadd16")
    await h.press(ADMIN_ID, "adm", "br:random")
    assert await _count(pool, "SELECT count(*) FROM matches") == 16  # 15 games + champion box
    leaf = await _count(pool, "SELECT id FROM matches WHERE stage_code = 'r8' LIMIT 1")
    parent = await _count(pool, "SELECT next_match_id FROM matches WHERE id = " + str(leaf))
    await h.press(ADMIN_ID, "adm", f"br:del:{parent}")  # a match other matches point at
    assert await _count(pool, f"SELECT count(*) FROM matches WHERE id = {parent}") == 0
    await h.press(ADMIN_ID, "adm", "br:clear")
    assert await _count(pool, "SELECT count(*) FROM matches") == 0


async def test_br_win_with_foreign_team_is_refused(h, pool):
    await _bracket(pool, 4)
    s1, s2 = await _rows(pool, "semi")
    await h.press(ADMIN_ID, "adm", f"br:win:{s1['id']}:{s2['team_a_id']}")
    assert "не играет" in h.last()
    assert (await _rows(pool, "semi"))[0]["winner_id"] is None


def _last_datas(h) -> list[str]:
    """callback_data of the most recent inline keyboard the bot showed."""
    for m in reversed(h.tg.requests):
        rm = getattr(m, "reply_markup", None)
        if rm is not None and getattr(rm, "inline_keyboard", None):
            return [b.callback_data for row in rm.inline_keyboard for b in row]
    return []


async def test_played_pair_disappears_from_editor(h, pool):
    await _bracket(pool, 4)
    s1, s2 = await _rows(pool, "semi")
    await h.press(ADMIN_ID, "adm", "br:edit")
    assert f"br:win:{s1['id']}:{s1['team_a_id']}" in _last_datas(h)
    await h.press(ADMIN_ID, "adm", f"br:win:{s1['id']}:{s1['team_a_id']}")
    datas = _last_datas(h)
    assert not any(d.startswith(f"br:win:{s1['id']}:") or d == f"br:del:{s1['id']}" for d in datas)
    assert any(d.startswith(f"br:win:{s2['id']}:") for d in datas)  # the unplayed semi is still there
    assert "Сыграно пар: 1" in h.last()


# ------------------------------------------------------------------ 3rd-place checkbox
async def _play_semis(pool):
    s1, s2 = await _rows(pool, "semi")
    await set_winner(pool, s1["id"], s1["team_a_id"])
    await set_winner(pool, s2["id"], s2["team_a_id"])
    return s1, s2


async def test_checkbox_on_by_default_and_shown(h, pool):
    await _bracket(pool, 4)
    await h.press(ADMIN_ID, "adm", "br:edit")
    assert "br:third:off" in _last_datas(h)  # ticked -> the button offers to switch it off
    assert "☑️ Матч за 3-е место" in h.buttons()


async def test_unticked_no_third_place_match_and_podium_complete_without_it(h, pool):
    from app.services.tournament import TournamentService
    await _bracket(pool, 4)
    await h.press(ADMIN_ID, "adm", "br:third:off")
    assert "br:third:on" in _last_datas(h)
    assert "не разыгрывается" in h.last()
    await _play_semis(pool)
    assert await _rows(pool, "third") == []  # not created
    (final,) = await _rows(pool, "final")
    await set_winner(pool, final["id"], final["team_a_id"])
    p = await TournamentService(pool).placements()
    assert p.ready and p.third_id is None  # finishing does not need an early close


async def test_untick_after_creation_deletes_it_and_retick_recreates(h, pool):
    await _bracket(pool, 4)
    s1, s2 = await _play_semis(pool)
    (third,) = await _rows(pool, "third")
    await set_winner(pool, third["id"], third["team_a_id"])
    await h.press(ADMIN_ID, "adm", "br:third:off")
    assert await _rows(pool, "third") == []
    await h.press(ADMIN_ID, "adm", "br:third:on")
    (again,) = await _rows(pool, "third")
    assert {again["team_a_id"], again["team_b_id"]} == {s1["team_b_id"], s2["team_b_id"]}
    assert again["status"] != "finished"


async def test_unticked_hides_third_stage_in_manual_pairing(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Bravo")
    await h.press(ADMIN_ID, "adm", "br:third:off")
    await h.press(ADMIN_ID, "adm", f"br:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"br:pick:{b}")
    assert "br:stage:third" not in _last_datas(h)
    await h.press(ADMIN_ID, "adm", "br:stage:third")  # stale/forged button
    assert await _rows(pool, "third") == []


async def test_new_tournament_starts_ticked(pool):
    from app.services.registration import TgUser
    from app.services.tournament import TournamentService
    from tests.helpers import add_user
    async with pool.acquire() as c:
        await c.execute("UPDATE tournaments SET third_place_enabled = 0 WHERE status = 'active'")
    await add_user(pool, 999, "theadmin")
    await TournamentService(pool).finalize(TgUser(999, "theadmin", "A"), force=True)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT third_place_enabled FROM tournaments WHERE status = 'active'") == 1
