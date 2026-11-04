import asyncio

import pytest

from app.services.admin import AdminService
from app.services.bracket import render_bracket
from app.services.errors import InvalidGameWinner, NotFound, SeriesDecided
from app.services.errors import Conflict, ValidationError
from app.services.registration import RegistrationService, TgUser
from app.services.series import SeriesService
from app.services.team import TeamService
from tests.helpers import make_team

pytestmark = pytest.mark.db


class FakeBot:
    """Enough of aiogram.Bot for SeriesService's best-effort result notifications."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def three(pool):
    return [await make_team(pool, i, f"Team {i}") for i in (1, 2, 3)]


async def play_bo5(series: SeriesService, mid: int, winner: int, loser: int, wins: int = 3, losses: int = 0):
    """Feeds `wins` games to `winner` and `losses` to `loser`, alternating, ending exactly when decided."""
    actor = TgUser(1, "actor0001", "Actor")
    seq, w, l = [], 0, 0
    while w < wins or l < losses:
        if w < wins:
            seq.append(winner)
            w += 1
        if l < losses:
            seq.append(loser)
            l += 1
    view = None
    for team in seq:
        view = await series.add_game(actor, mid, team)
        if view.match["status"] == "finished":
            break
    return view


async def test_create_match_validations(pool):
    adm = AdminService(pool)
    a, b, c = await three(pool)
    with pytest.raises(ValidationError):
        await adm.create_match(a, a, "r8")
    with pytest.raises(ValidationError):
        await adm.create_match(a, b, "unknown")
    with pytest.raises(NotFound):
        await adm.create_match(a, 99999, "r8")
    mid = await adm.create_match(a, b, "r8")
    m = await adm.match(mid)
    assert (m["status"], m["winner_id"], m["stage_title"]) == ("scheduled", None, "1/8")
    with pytest.raises(Conflict):
        await adm.create_match(b, a, "r8")   # same pair, reversed
    await adm.create_match(a, b, "final")    # other stage fine


async def test_concurrent_match_creation_single_row(pool):
    adm = AdminService(pool)
    a, b, _ = await three(pool)
    res = await asyncio.gather(*[adm.create_match(a, b, "semi") for _ in range(20)], return_exceptions=True)
    assert sum(not isinstance(r, BaseException) for r in res) == 1
    assert all(isinstance(r, Conflict) for r in res if isinstance(r, BaseException))


async def test_bo5_winner_computed_from_games(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b, c = await three(pool)
    mid = await adm.create_match(a, b, "r4")
    actor = TgUser(1, "actor0001", "Actor")
    with pytest.raises(InvalidGameWinner):
        await series.add_game(actor, mid, c)   # not one of the two series participants
    with pytest.raises(NotFound):
        await series.add_game(actor, 99999, a)
    view = await play_bo5(series, mid, winner=b, loser=a, wins=3, losses=0)
    assert view.match["status"] == "finished" and view.match["winner_id"] == b and view.match["winner"] == "Team 2"
    assert (view.wins_a, view.wins_b) == (0, 3) and len(view.games) == 3
    m = await adm.match(mid)
    assert m["winner_id"] == b
    with pytest.raises(SeriesDecided):        # a 4th game is refused: the series is already decided
        await series.add_game(actor, mid, a)
    async with pool.acquire() as conn:        # no automatic next round
        assert await conn.fetchval("SELECT count(*) FROM matches") == 1
        assert await conn.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", mid) == 3


@pytest.mark.parametrize("wins,losses,total", [(3, 0, 3), (3, 1, 4), (3, 2, 5)])
async def test_bo5_all_valid_final_scores(pool, wins, losses, total):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b, _ = await three(pool)
    mid = await adm.create_match(a, b, "r4")
    view = await play_bo5(series, mid, winner=a, loser=b, wins=wins, losses=losses)
    assert view.match["status"] == "finished" and view.match["winner_id"] == a
    assert len(view.games) == total and view.wins_a == wins and view.wins_b == losses


async def test_bo5_game5_only_at_two_all_never_a_sixth_game(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b, _ = await three(pool)
    mid = await adm.create_match(a, b, "r4")
    actor = TgUser(1, "actor0001", "Actor")
    for team in (a, b, a, b):  # 2:2
        await series.add_game(actor, mid, team)
    view = await series.add_game(actor, mid, a)  # game 5 decides it
    assert view.match["status"] == "finished" and len(view.games) == 5
    with pytest.raises(SeriesDecided):
        await series.add_game(actor, mid, b)  # no game 6


async def test_bo5_concurrent_game_submissions_never_exceed_five_or_flip_decided_series(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b, _ = await three(pool)
    mid = await adm.create_match(a, b, "r4")
    res = await asyncio.gather(*[series.add_game(TgUser(1, "actor0001"), mid, a if i % 2 else b) for i in range(20)], return_exceptions=True)
    ok = [r for r in res if not isinstance(r, BaseException)]
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", mid)
    assert n <= 5
    assert len(ok) == n
    assert all(isinstance(r, SeriesDecided) for r in res if isinstance(r, BaseException))


async def test_create_match_requires_two_players_per_team(pool):
    from app.services.errors import TeamNotEligible
    from app.services.team import TeamService

    adm, teams = AdminService(pool), TeamService(pool)
    empty = await teams.create_team(TgUser(1, "regist01", "Reg"), "Empty Team", "registrar")   # 0/5
    solo_capt = await teams.create_team(TgUser(2, "cap0002", "Cap"), "Solo Captain", "captain")  # 1/5
    ready_a = await make_team(pool, 5, size=2)  # 2/5
    ready_b = await make_team(pool, 6, size=2)  # 2/5
    for other in (solo_capt.team_id, ready_a):
        with pytest.raises(TeamNotEligible):
            await adm.create_match(empty.team_id, other, "r8")
    with pytest.raises(TeamNotEligible):
        await adm.create_match(solo_capt.team_id, ready_a, "r8")
    mid = await adm.create_match(ready_a, ready_b, "r8")  # 2/5 is enough on both sides
    assert mid


async def test_team_delete_blocked_by_matches_then_allowed(pool):
    adm = AdminService(pool)
    a, b, c = await three(pool)
    mid = await adm.create_match(a, b, "r8")
    with pytest.raises(Conflict):
        await adm.delete_team(a)
    await adm.delete_match(mid)
    await adm.delete_team(a)
    with pytest.raises(NotFound):
        await adm.delete_team(a)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM team_members") == 10


async def test_solo_admin_ops(pool):
    adm, reg = AdminService(pool), RegistrationService(pool)
    await reg.register_solo(TgUser(1, "solo0001"), "EXP")
    await reg.register_solo(TgUser(2, "solo0002"), "GOLD")
    total, page, last, rows = await adm.solo_page(0)
    assert (total, last, len(rows)) == (2, 0, 2)
    await adm.delete_solo(1)
    with pytest.raises(NotFound):
        await adm.delete_solo(1)
    assert (await adm.solo_page(0))[0] == 1
    await reg.register_solo(TgUser(1, "solo0001"), "MID")  # can register again after removal


async def test_pagination_clamps(pool):
    adm = AdminService(pool)
    for i in range(1, 12):
        await make_team(pool, i)
    total, page, last, rows = await adm.teams_page(0)
    assert (total, page, last, len(rows)) == (11, 0, 1, 8)
    total, page, last, rows = await adm.teams_page(999)
    assert (page, len(rows)) == (1, 3)
    assert (await adm.teams_page(-5))[1] == 0


async def test_bracket_order_and_extensible_stages(pool):
    adm = AdminService(pool)
    a, b, c = await three(pool)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO stages (code,title,sort_order) VALUES ('r32','1/32',5)")
    await adm.create_match(a, b, "final")
    await adm.create_match(a, c, "r32")
    await adm.create_match(b, c, "r8")
    text = "".join(render_bracket(await adm.bracket_rows()))
    assert text.index("1/32") < text.index("1/8") < text.index("Финал")


async def test_bracket_with_html_names(pool):
    teams_svc = TeamService(pool)
    v1 = await teams_svc.create_team(TgUser(1, "capone1", "C1"), "<b>Evil</b>")
    await teams_svc.join_team(TgUser(3, "mate0003", "M3"), v1.invite_token)  # min 2 for tournament play
    v2 = await teams_svc.create_team(TgUser(2, "captwo2", "C2"), "Good & Co")
    await teams_svc.join_team(TgUser(4, "mate0004", "M4"), v2.invite_token)
    adm = AdminService(pool)
    teams = await adm.all_teams()
    await adm.create_match(teams[0]["id"], teams[1]["id"], "r8")
    text = "".join(render_bracket(await adm.bracket_rows()))
    assert "<b>Evil</b>" not in text and "&lt;b&gt;Evil" in text and "Good &amp; Co" in text
