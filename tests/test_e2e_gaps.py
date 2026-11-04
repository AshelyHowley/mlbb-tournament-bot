"""Gap-filling handler-level E2E, driven through the real dispatcher with a fake Telegram
transport (no network, no real sends): team match-eligibility boundaries, notification recipient
counts across variable team sizes with the registrar excluded, photo/video/document notification
delivery, retry-failed-recipients, ACTIVE DEPUTY archiving a tournament, and a concurrent archive
race between MAIN ADMIN and ACTIVE DEPUTY on the same tournament."""
from __future__ import annotations

import asyncio
import re

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage as SendMessageMethod

from app.services.registration import TgUser
from app.services.team import TeamService
from tests.harness import ADMIN_ID, Harness
from tests.helpers import make_team

pytestmark = pytest.mark.db

DEP = (1001, "deputy001")
DEP_TOKEN_RE = re.compile(r"start=deputy_([A-Za-z0-9_-]{32,48})")


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


async def make_deputy(h, pool) -> None:
    """Drives the real invite->claim->approve UI flow so DEP becomes an active deputy."""
    await h.press(ADMIN_ID, "theadmin", "dep:menu")
    await h.press(ADMIN_ID, "theadmin", "dep:invite")
    token = DEP_TOKEN_RE.search(h.last()).group(1)
    h.clear()
    await h.say(*DEP, f"/start deputy_{token}")
    await h.press(*DEP, f"dep:acc:{token}")
    async with pool.acquire() as c:
        invite_id = await c.fetchval("SELECT id FROM deputy_invites WHERE candidate_id = $1", DEP[0])
    h.clear()
    await h.press(ADMIN_ID, "theadmin", "dep:pending")
    await h.press(ADMIN_ID, "theadmin", f"dep:approve:{invite_id}")


async def _create_match(h, actor_id, actor_uname, a, b, stage) -> None:
    await h.press(actor_id, actor_uname, "mt:new")
    await h.press(actor_id, actor_uname, f"mt:a:pick:{a}")
    await h.press(actor_id, actor_uname, f"mt:b:pick:{b}")
    await h.press(actor_id, actor_uname, f"mt:stage:{stage}")
    await h.press(actor_id, actor_uname, "mt:ok")


async def _win_bo5(h, actor_id, actor_uname, pool, winner) -> int:
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches ORDER BY id DESC LIMIT 1")
    for i in range(3):
        await h.press(actor_id, actor_uname, f"mt:gnew:{mid}")
        await h.press(actor_id, actor_uname, f"mt:gw:{mid}:{winner}")
        await h.press(actor_id, actor_uname, f"mt:gok:{mid}:{winner}:{i + 1}")
    return mid


async def _prepare_full_podium(h, actor_id, actor_uname, pool) -> None:
    """Round -> semi-final -> final + third-place, all created and played manually (no draw)."""
    a, b, c, d = [await make_team(pool, i, f"Team {chr(64 + i)}") for i in (1, 2, 3, 4)]

    async def win(x, y, stage, winner):
        await _create_match(h, actor_id, actor_uname, x, y, stage)
        await _win_bo5(h, actor_id, actor_uname, pool, winner)

    await win(a, b, "semi", a)
    await win(c, d, "semi", d)
    await win(b, c, "third", b)
    await win(a, d, "final", a)


# ------------------------------------------------------------------ team match eligibility boundaries
async def test_team_match_eligibility_boundaries_at_handler_level(h, pool):
    teams = TeamService(pool)
    empty = (await teams.create_team(TgUser(1, "regist01", "Reg"), "Empty Team", "registrar")).team_id      # 0/5
    solo_capt = (await teams.create_team(TgUser(2, "cap0002", "Cap"), "Solo Captain", "captain")).team_id   # 1/5
    ready_a = await make_team(pool, 10, size=2)   # 2/5
    ready_b = await make_team(pool, 11, size=2)   # 2/5

    await _create_match(h, ADMIN_ID, "adm", empty, ready_a, "r8")
    assert "минимум 2 игрока" in h.last()
    h.clear()

    await _create_match(h, ADMIN_ID, "adm", solo_capt, ready_a, "r8")
    assert "минимум 2 игрока" in h.last()
    h.clear()

    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM matches") == 0

    await _create_match(h, ADMIN_ID, "adm", ready_a, ready_b, "r8")
    assert "создан" in h.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM matches") == 1


# ------------------------------------------------------------------ notification recipients: variable size, registrar excluded
async def _team_with_registrar(pool, base_id: int, size: int) -> int:
    """A registrar-only creator (never a team_members row, so never a notification recipient)
    plus `size` real players (captain via captain-invite + joiners via player-invite)."""
    teams = TeamService(pool)
    view = await teams.create_team(TgUser(base_id, f"reg{base_id}", "Registrar"), f"RegTeam{base_id}", "registrar")
    await teams.accept_captain(TgUser(base_id + 1, f"cap{base_id}", "Captain"), view.captain_invite_token)
    for k in range(2, size + 1):
        await teams.join_team(TgUser(base_id + k, f"mem{base_id}_{k}", f"M{k}"), view.invite_token)
    return view.team_id


@pytest.mark.parametrize("size", [2, 3, 4, 5])
async def test_notification_recipient_count_matches_real_roster_registrar_excluded(h, pool, size):
    a = await _team_with_registrar(pool, 100 + size * 10, size)
    b = await make_team(pool, 900 + size, size=2)
    await _create_match(h, ADMIN_ID, "adm", a, b, "r8")
    await _win_bo5(h, ADMIN_ID, "adm", pool, a)
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:aud:series_winners:{mid}")
    await h.say(ADMIN_ID, "adm", "Поздравляем!")
    assert f"Получателей: <b>{size}</b>" in " ".join(h.texts())


# ------------------------------------------------------------------ photo/video/document notification via fake Telegram API
@pytest.mark.parametrize("kind,title", [("photo", "фото"), ("video", "видео"), ("document", "документ")])
async def test_notification_media_flow_through_fake_telegram(h, pool, kind, title):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")  # a: captain 10 + members 11-14
    await _create_match(h, ADMIN_ID, "adm", a, b, "r8")
    await _win_bo5(h, ADMIN_ID, "adm", pool, a)
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    h.clear()
    file_id = f"FAKE_FILE_{kind.upper()}"
    await h.press(ADMIN_ID, "adm", f"sn:aud:series_winners:{mid}")
    await h.media(ADMIN_ID, "adm", kind, file_id, caption="Смотрите итоги!")
    combined = " ".join(h.texts())
    assert "ПРЕДПРОСМОТР" in combined and f"Тип: <b>{title}</b>" in combined
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT id, content_type, file_id FROM result_broadcasts")
    assert row["content_type"] == kind and row["file_id"] == file_id
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:send:{row['id']}")
    await asyncio.gather(*h.svc.result_notify._tasks)
    async with pool.acquire() as c:
        finished = await c.fetchrow("SELECT status, sent FROM result_broadcasts WHERE id=$1", row["id"])
    assert finished["status"] == "done" and finished["sent"] == 5
    team_member_ids = {10, 11, 12, 13, 14}
    delivered = [m for m in h.tg.requests if getattr(m, kind, None) == file_id and m.chat_id in team_member_ids]
    assert len(delivered) == 5  # exactly the 5 real teammates, not the 1 admin-preview send


# ------------------------------------------------------------------ retry failed recipients
async def test_notification_retry_failed_recipients(h, pool):
    a = await make_team(pool, 1, "Alpha", size=2)  # captain 10 + member 11
    b = await make_team(pool, 2, "Beta", size=2)
    await _create_match(h, ADMIN_ID, "adm", a, b, "r8")
    await _win_bo5(h, ADMIN_ID, "adm", pool, a)
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    h.tg.fail_send[11] = TelegramBadRequest(method=SendMessageMethod(chat_id=11, text="x"), message="chat not found")
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:aud:series_winners:{mid}")
    await h.say(ADMIN_ID, "adm", "Поздравляем!")
    async with pool.acquire() as c:
        rid = await c.fetchval("SELECT id FROM result_broadcasts")
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:send:{rid}")
    await asyncio.gather(*h.svc.result_notify._tasks)
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT status, sent, failed FROM result_broadcasts WHERE id=$1", rid)
    assert (row["status"], row["sent"], row["failed"]) == ("done", 1, 1)
    assert any(f"Рассылка #{rid}" in t and "Ошибок: 1" in t for t in h.texts())
    assert "🔄 Повторить неотправленным" in h.buttons()

    del h.tg.fail_send[11]  # recipient reachable now
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:retry:{rid}")
    await asyncio.gather(*h.svc.result_notify._tasks)
    async with pool.acquire() as c:
        row2 = await c.fetchrow("SELECT status, sent, failed FROM result_broadcasts WHERE id=$1", rid)
    assert (row2["status"], row2["sent"], row2["failed"]) == ("done", 2, 0)


# ------------------------------------------------------------------ full bracket + placements, ACTIVE DEPUTY archive
async def test_full_bracket_semifinal_routing_and_placements_via_handlers(h, pool):
    await _prepare_full_podium(h, ADMIN_ID, "adm", pool)
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM tournaments WHERE status='active'")
        row = await c.fetchrow(
            "SELECT team_a_id, team_b_id, winner_id, stage_code FROM matches WHERE stage_code='final'"
        )
        third_row = await c.fetchrow(
            "SELECT winner_id FROM matches WHERE stage_code='third'"
        )
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:start")
    assert "Все призовые места определены" in h.last()
    await h.press(ADMIN_ID, "adm", "tf:go")
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    await h.press(ADMIN_ID, "adm", "tf:ok")
    assert any("ТУРНИР ЗАВЕРШЁН" in t for t in h.texts())  # a 2nd, final-broadcast-offer message follows it
    async with pool.acquire() as c:
        t = await c.fetchrow("SELECT status, first_place_team_id, second_place_team_id, third_place_team_id FROM tournaments WHERE id=$1", tid)
    assert t["status"] == "completed"
    assert t["first_place_team_id"] == row["winner_id"]
    loser = row["team_b_id"] if row["winner_id"] == row["team_a_id"] else row["team_a_id"]
    assert t["second_place_team_id"] == loser
    assert t["third_place_team_id"] == third_row["winner_id"]


async def test_active_deputy_can_finalize_tournament(h, pool):
    await make_deputy(h, pool)
    await _prepare_full_podium(h, *DEP, pool)
    h.clear()
    await h.press(*DEP, "tf:start")
    await h.press(*DEP, "tf:go")
    await h.say(*DEP, "ЗАВЕРШИТЬ")
    await h.press(*DEP, "tf:ok")
    assert any("ТУРНИР ЗАВЕРШЁН" in t for t in h.texts())  # a 2nd, final-broadcast-offer message follows it
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments") == 2
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status='active'") == 1
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status='completed'") == 1


async def test_finish_ok_handles_lost_active_tournament_race_gracefully(h, pool):
    """Regression guard for the admin_panel.py fix: finish_ok used to catch only TournamentNotReady,
    so the rare race where the active tournament disappears between the placements check and the
    actual finalize() call (another staff member's concurrent finalize won it a moment earlier)
    raised an uncaught NotFound past the handler instead of degrading gracefully. Deterministic:
    the active row is flipped out from under the flow by raw SQL right before the final tf:ok."""
    await make_team(pool, 1, "Alpha")
    await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "tf:start")
    await h.press(ADMIN_ID, "adm", "tf:early")
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    async with pool.acquire() as c:
        await c.execute("UPDATE tournaments SET status='closed_early', completed_at=datetime('now') WHERE status='active'")
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:ok")  # must not raise past the handler (caught by the `h` fixture teardown too)
    assert h.texts()  # the operator is shown a message, not left hanging on a silent crash
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status='active'") == 0


async def test_concurrent_archive_by_admin_and_deputy_yields_exactly_one_active(h, pool):
    await make_deputy(h, pool)
    await _prepare_full_podium(h, ADMIN_ID, "adm", pool)
    async with pool.acquire() as c:
        before_tid = await c.fetchval("SELECT id FROM tournaments WHERE status='active'")
    h.clear()
    # both staff members independently drive their own FSM up to "confirmed" (per-user state)
    await h.press(ADMIN_ID, "adm", "tf:start")
    await h.press(ADMIN_ID, "adm", "tf:go")
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    await h.press(*DEP, "tf:start")
    await h.press(*DEP, "tf:go")
    await h.say(*DEP, "ЗАВЕРШИТЬ")
    h.clear()
    res = await asyncio.gather(h.press(ADMIN_ID, "adm", "tf:ok"), h.press(*DEP, "tf:ok"), return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in res), res  # handler always degrades gracefully
    async with pool.acquire() as c:
        active_count = await c.fetchval("SELECT count(*) FROM tournaments WHERE status='active'")
        archived_ids = [r["id"] for r in await c.fetch("SELECT id FROM tournaments WHERE status <> 'active'")]
    assert active_count == 1
    assert before_tid in archived_ids
    assert len(archived_ids) == len(set(archived_ids))  # the original tournament was never archived twice


# ------------------------------------------------------------------ REMOVED/INACTIVE deputy denied everywhere staff-only
async def test_removed_deputy_denied_archive_and_notify(h, pool):
    await make_deputy(h, pool)
    async with pool.acquire() as c:
        uid = await c.fetchval("SELECT user_id FROM deputies WHERE revoked_at IS NULL")
    await h.press(ADMIN_ID, "theadmin", f"dep:revoke:{uid}")
    await make_team(pool, 1, "Alpha")
    await make_team(pool, 2, "Beta")
    h.clear()
    for data in ("tf:start", "tf:ok", "sn:pick:1", "mt:new", "hist:0"):
        await h.press(*DEP, data)
    assert h.texts() == []
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments") == 1
        assert await c.fetchval("SELECT count(*) FROM matches") == 0
