"""BO5 series: games, score derived from games, editing rules, and team-size 2-5 (not just 5)."""
import asyncio

# import asyncpg  # removed for SQLite
import pytest

from app.services.admin import AdminService
from app.services.errors import InvalidGameWinner, NotFound, ResultLocked, SeriesDecided, TeamNotEligible
from app.services.registration import TgUser
from app.services.series import SeriesService
from app.services.team import TeamService
from tests.helpers import make_team

pytestmark = pytest.mark.db


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


ACTOR = TgUser(1, "actor0001", "Actor")


async def two_teams(pool, size_a=5, size_b=5):
    a = await make_team(pool, 1, "Alpha", size=size_a)
    b = await make_team(pool, 2, "Beta", size=size_b)
    return a, b


# ---------------------------------------------------------------- team size 2-5 for tournament play
async def test_team_registration_allows_0_and_1(pool):
    teams = TeamService(pool)
    empty = await teams.create_team(TgUser(1, "regist01", "Reg"), "Empty", "registrar")
    solo = await teams.create_team(TgUser(2, "cap0002", "Cap"), "Solo", "captain")
    assert empty.size == 0 and solo.size == 1  # registration itself never rejects these


@pytest.mark.parametrize("size", [2, 3, 4, 5])
async def test_team_sizes_2_to_5_are_tournament_ready(pool, size):
    adm = AdminService(pool)
    a = await make_team(pool, 1, "Alpha", size=size)
    b = await make_team(pool, 2, "Beta", size=5)
    mid = await adm.create_match(a, b, "r8")
    assert mid


@pytest.mark.parametrize("size", [0, 1])
async def test_team_sizes_below_2_are_tournament_denied(pool, size):
    adm, teams = AdminService(pool), TeamService(pool)
    role = "registrar" if size == 0 else "captain"
    not_ready = await teams.create_team(TgUser(1, "regist01", "Reg"), "NotReady", role)
    ready = await make_team(pool, 2, "Ready", size=5)
    with pytest.raises(TeamNotEligible) as e:
        await adm.create_match(not_ready.team_id, ready, "r8")
    assert "не готова" in e.value.user_message.lower() or "минимум 2" in e.value.user_message


async def test_six_members_is_impossible_regardless_of_tournament_layer(pool):
    """Sanity check that the pre-existing max-5 guard (migration 002/003) still holds after 004."""
    tid = await make_team(pool, 1, size=5)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (999, 'Six')")
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 999)", tid)
        assert e.value.sqlstate == "MB003"


# ---------------------------------------------------------------- BO5 scoring
@pytest.mark.parametrize("wins_a,wins_b,total", [(3, 0, 3), (3, 1, 4), (3, 2, 5), (0, 3, 3), (1, 3, 4), (2, 3, 5)])
async def test_bo5_final_scores(pool, wins_a, wins_b, total):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    seq = []
    ca = cb = 0
    while ca < wins_a or cb < wins_b:
        if ca < wins_a:
            seq.append(a)
            ca += 1
        if cb < wins_b:
            seq.append(b)
            cb += 1
    view = None
    for team in seq:
        view = await series.add_game(ACTOR, mid, team)
        if view.match["status"] == "finished":
            break
    winner = a if wins_a == 3 else b
    assert view.match["winner_id"] == winner and len(view.games) == total
    assert (view.wins_a, view.wins_b) == (wins_a, wins_b)


async def test_no_sixth_game_and_no_game_after_3_0(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    for _ in range(3):
        await series.add_game(ACTOR, mid, a)  # 3:0
    with pytest.raises(SeriesDecided):
        await series.add_game(ACTOR, mid, b)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", mid) == 3


async def test_no_game5_before_2_2_but_allowed_at_2_2(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    for team in (a, b, a, b):  # 2:2
        await series.add_game(ACTOR, mid, team)
    view = await series.add_game(ACTOR, mid, a)
    assert len(view.games) == 5 and view.match["status"] == "finished"


async def test_invalid_winner_rejected(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    c = await make_team(pool, 3, "Gamma")
    mid = await adm.create_match(a, b, "r8")
    with pytest.raises(InvalidGameWinner):
        await series.add_game(ACTOR, mid, c)
    with pytest.raises(NotFound):
        await series.add_game(ACTOR, 99999, a)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM match_games") == 0


async def test_score_is_always_derived_never_stored_directly(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    for team in (a, a, b):
        await series.add_game(ACTOR, mid, team)
    view = await series.series_view(mid)
    assert view.wins_a == 2 and view.wins_b == 1
    async with pool.acquire() as c:
        cols = {r["column_name"] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'matches'")}
    assert "wins_a" not in cols and "score" not in cols  # no redundant score column exists at all


async def test_advance_only_after_3_wins_not_before(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    await series.add_game(ACTOR, mid, a)
    view = await series.series_view(mid)
    assert view.match["status"] == "scheduled" and view.match["winner_id"] is None
    await series.add_game(ACTOR, mid, a)
    await series.add_game(ACTOR, mid, a)
    view = await series.series_view(mid)
    assert view.match["status"] == "finished" and view.match["winner_id"] == a


# ---------------------------------------------------------------- correcting a game
async def test_edit_game_before_completion_recomputes_score(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    v1 = await series.add_game(ACTOR, mid, a)
    game_id = v1.games[0]["id"]
    await series.add_game(ACTOR, mid, a)
    v = await series.edit_game(ACTOR, game_id, b)  # game 1 corrected to B: now 1:1
    assert v.wins_a == 1 and v.wins_b == 1 and v.match["status"] == "scheduled"


async def test_edit_completed_series_undoes_completion_if_score_changes(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    v = None
    game_ids = []
    for team in (a, a, a):
        v = await series.add_game(ACTOR, mid, team)
        game_ids.append(v.games[-1]["id"])
    assert v.match["status"] == "finished" and v.match["winner_id"] == a
    v = await series.edit_game(ACTOR, game_ids[0], b)  # now 2:1, no longer decided
    assert v.match["status"] == "scheduled" and v.match["winner_id"] is None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM matches WHERE id = $1", mid) == "scheduled"


async def test_edit_completed_series_can_flip_the_winner(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    v = None
    game_ids = []
    for team in (a, a, a):
        v = await series.add_game(ACTOR, mid, team)
        game_ids.append(v.games[-1]["id"])
    for gid in game_ids:
        v = await series.edit_game(ACTOR, gid, b)
    assert v.match["status"] == "finished" and v.match["winner_id"] == b and v.wins_b == 3


async def test_edit_locked_once_a_team_advanced_to_another_match(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    c = await make_team(pool, 3, "Gamma")
    mid = await adm.create_match(a, b, "semi")
    v = None
    game_ids = []
    for team in (a, a, a):
        v = await series.add_game(ACTOR, mid, team)
        game_ids.append(v.games[-1]["id"])
    await adm.create_match(a, c, "final")  # a has now "advanced" - a new match features it
    with pytest.raises(ResultLocked):
        await series.edit_game(ACTOR, game_ids[0], b)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT winner_id FROM matches WHERE id = $1", mid) == a  # unchanged


async def test_edit_not_locked_when_no_downstream_match_exists(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "semi")
    v = None
    game_ids = []
    for team in (a, a, a):
        v = await series.add_game(ACTOR, mid, team)
        game_ids.append(v.games[-1]["id"])
    v = await series.edit_game(ACTOR, game_ids[0], b)  # no other match references a or b yet: allowed
    assert v is not None


async def conn_count(c, mid):
    return await c.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", mid)


async def test_double_click_confirm_same_game_is_idempotent_not_two_games(pool):
    """Rapidly double-tapping the "confirm game N" button (same expected_game_number, from the same
    stale confirm screen) must record exactly ONE game, not silently advance the series by two."""
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    res = await asyncio.gather(*[series.add_game(ACTOR, mid, a, expected_game_number=1) for _ in range(8)])
    async with pool.acquire() as c:
        n = await conn_count(c, mid)
    assert n == 1  # every one of the 8 calls reports a consistent 1-game state, only one row exists
    assert all(v.wins_a == 1 and len(v.games) == 1 for v in res)


async def test_repeated_confirm_after_advancing_is_a_stale_noop(pool):
    """A user who confirms game 1, then (network lag) confirms the STALE game-1 screen again after
    someone else already recorded game 2, must not create an out-of-order duplicate."""
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    await series.add_game(ACTOR, mid, a, expected_game_number=1)
    await series.add_game(ACTOR, mid, b, expected_game_number=2)
    v = await series.add_game(ACTOR, mid, a, expected_game_number=1)  # stale screen replayed
    assert len(v.games) == 2 and v.wins_a == 1 and v.wins_b == 1  # unchanged, no 3rd game inserted


async def test_expected_game_number_not_required_for_programmatic_callers(pool):
    """Callers that don't track a specific slot (tests, scripts) can omit expected_game_number and
    always append the next game, exactly like before this guard existed."""
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    for _ in range(3):
        await series.add_game(ACTOR, mid, a)
    async with pool.acquire() as c:
        assert await conn_count(c, mid) == 3


async def test_concurrent_result_submissions_never_corrupt_the_series(pool):
    adm, series = AdminService(pool), SeriesService(pool, FakeBot())
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "r8")
    res = await asyncio.gather(*[series.add_game(ACTOR, mid, a if i % 2 else b) for i in range(50)], return_exceptions=True)
    async with pool.acquire() as c:
        n = await conn_count(c, mid)
        wins_a = await c.fetchval("SELECT count(*) FROM match_games WHERE match_id=$1 AND winner_team_id=$2", mid, a)
        wins_b = await c.fetchval("SELECT count(*) FROM match_games WHERE match_id=$1 AND winner_team_id=$2", mid, b)
        game_numbers = [r["game_number"] for r in await c.fetch("SELECT game_number FROM match_games WHERE match_id=$1 ORDER BY 1", mid)]
    assert n <= 5 and wins_a <= 3 and wins_b <= 3
    assert game_numbers == list(range(1, n + 1))  # no gaps, no duplicates
    assert sum(not isinstance(r, BaseException) for r in res) == n
    assert all(isinstance(r, SeriesDecided) for r in res if isinstance(r, BaseException))


# ---------------------------------------------------------------- notifications
async def test_notification_sent_only_once_series_completes_not_per_game(pool):
    bot = FakeBot()
    adm, series = AdminService(pool), SeriesService(pool, bot)
    a, b = await two_teams(pool, size_a=3, size_b=2)  # variable team sizes
    mid = await adm.create_match(a, b, "r8")
    await series.add_game(ACTOR, mid, a)
    assert bot.sent == []  # not after the first game
    await series.add_game(ACTOR, mid, a)
    assert bot.sent == []  # not after the second
    await series.add_game(ACTOR, mid, a)  # decisive
    assert len(bot.sent) == 5  # 3 winners + 2 losers, real team_members only


async def test_notification_recipients_match_real_variable_roster_captain_included_registrar_excluded(pool):
    bot = FakeBot()
    adm, series, teams = AdminService(pool), SeriesService(pool, bot), TeamService(pool)
    a = await make_team(pool, 1, "Alpha", size=2)   # captain(10) + 1 member(11)
    b_view = await teams.create_team(TgUser(500, "regist05", "Reg"), "Beta", "member")  # registrar+member, no captain yet
    await teams.accept_captain(TgUser(501, "cap0501", "Cap"), b_view.captain_invite_token)
    await teams.join_team(TgUser(502, None, "P2"), b_view.invite_token)
    mid = await adm.create_match(a, b_view.team_id, "r8")
    for _ in range(3):
        await series.add_game(ACTOR, mid, a)
    recipients = {uid for uid, _ in bot.sent}
    assert recipients == {10, 11, 500, 501, 502}  # registrar 500 IS also a member here, so included
    assert len(bot.sent) == 5


async def test_notification_registrar_only_excluded_when_not_a_player(pool):
    bot = FakeBot()
    adm, series, teams = AdminService(pool), SeriesService(pool, bot), TeamService(pool)
    a = await make_team(pool, 1, "Alpha", size=2)
    b_view = await teams.create_team(TgUser(600, "regist06", "RegOnly"), "Beta", "registrar")  # 0/5, not a player
    await teams.accept_captain(TgUser(601, "cap0601", "Cap"), b_view.captain_invite_token)
    await teams.join_team(TgUser(602, None, "P2"), b_view.invite_token)
    mid = await adm.create_match(a, b_view.team_id, "r8")
    for _ in range(3):
        await series.add_game(ACTOR, mid, b_view.team_id)
    recipients = {uid for uid, _ in bot.sent}
    assert 600 not in recipients  # registrar-only, never a team_members row
    assert recipients == {10, 11, 601, 602}


async def test_notification_delivery_failure_isolated(pool):
    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if chat_id == 11:
                from aiogram.exceptions import TelegramForbiddenError

                from tests.helpers import dummy_method
                raise TelegramForbiddenError(dummy_method(), "blocked")
            await super().send_message(chat_id, text, **kw)

    bot = FlakyBot()
    adm, series = AdminService(pool), SeriesService(pool, bot)
    a, b = await two_teams(pool)  # 5-member teams: captain 10, members 11-14
    mid = await adm.create_match(a, b, "r8")
    for _ in range(3):
        await series.add_game(ACTOR, mid, a)
    recipients = {uid for uid, _ in bot.sent}
    assert 11 not in recipients and 10 in recipients and len(recipients) == 9  # one blocked, rest delivered


async def test_final_and_third_place_message_templates(pool):
    bot = FakeBot()
    adm, series = AdminService(pool), SeriesService(pool, bot)
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "final")
    for _ in range(3):
        await series.add_game(ACTOR, mid, a)
    texts = dict(bot.sent)
    assert "ЧЕМПИОНЫ" in texts[10] and "1 место" in texts[10]
    assert "2 место" in texts[20] or "2 МЕСТО" in texts.get(20, "")


async def test_semifinal_loser_told_they_play_for_third(pool):
    bot = FakeBot()
    adm, series = AdminService(pool), SeriesService(pool, bot)
    a, b = await two_teams(pool)
    mid = await adm.create_match(a, b, "semi")
    for _ in range(3):
        await series.add_game(ACTOR, mid, a)
    texts = dict(bot.sent)
    assert "финал" in texts[10].lower()
    assert "3 мест" in texts[20].lower()
