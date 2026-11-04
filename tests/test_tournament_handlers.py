"""UI-level: BO5 game entry, deputy invite/approve, history browsing, finish-tournament flow, and
staff (deputy) vs main-admin authorization, all through the real dispatcher."""
import asyncio
import re

import pytest
import pytest_asyncio

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


async def counts(pool):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT count(*) FROM teams"), await c.fetchval("SELECT count(*) FROM matches")


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


# ------------------------------------------------------------------ deputy invite/approve UI
async def test_deputy_invite_claim_approve_flow(h, pool):
    await h.press(ADMIN_ID, "theadmin", "dep:menu")
    assert "Заместители" in h.last()
    await h.press(ADMIN_ID, "theadmin", "dep:invite")
    token = DEP_TOKEN_RE.search(h.last()).group(1)
    h.clear()
    await h.say(*DEP, f"/start deputy_{token}")
    assert "Приглашение заместителя" in h.last()
    assert {"✅ Принять приглашение", "❌ Отказаться"} <= set(h.buttons())
    await h.press(*DEP, f"dep:acc:{token}")
    assert any("Заявка отправлена" in t for t in h.texts())
    assert any("Новый заместитель" in t for t in h.texts())  # admin was notified
    h.clear()
    await h.press(ADMIN_ID, "theadmin", "dep:pending")
    assert "Заявки на рассмотрении" in h.last()
    async with pool.acquire() as c:
        invite_id = await c.fetchval("SELECT id FROM deputy_invites WHERE candidate_id = $1", DEP[0])
    await h.press(ADMIN_ID, "theadmin", f"dep:review:{invite_id}")
    assert str(DEP[0]) in h.last()
    h.clear()
    await h.press(ADMIN_ID, "theadmin", f"dep:approve:{invite_id}")
    assert any("подтверждён" in t for t in h.texts())
    assert any("назначены заместителем" in t for t in h.texts())
    h.clear()
    await h.say(*DEP, "/start")
    assert "🛠 Админ-панель" in h.buttons()  # deputy now sees the admin panel entry


async def test_deputy_cannot_be_double_claimed(h, pool):
    await h.press(ADMIN_ID, "theadmin", "dep:invite")
    token = DEP_TOKEN_RE.search(h.last()).group(1)
    other = (1002, "other002")
    h.clear()
    await h.press(*DEP, f"dep:acc:{token}")
    await h.press(*other, f"dep:acc:{token}")
    assert "уже используется другим" in h.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT candidate_id FROM deputy_invites WHERE token = $1", token) == DEP[0]


async def test_deputy_cannot_manage_deputies(h, pool):
    await make_deputy(h, pool)
    h.clear()
    before_active = len((await pool.fetch("SELECT 1 FROM deputies WHERE revoked_at IS NULL")))
    for data in ("dep:menu", "dep:invite", "dep:list", "dep:pending", "dep:revoke:1"):
        await h.press(*DEP, data)
    shown = " ".join(h.texts())
    assert "Заместители" not in shown and "Пригласить" not in shown
    after_active = len((await pool.fetch("SELECT 1 FROM deputies WHERE revoked_at IS NULL")))
    assert before_active == after_active == 1


async def test_revoked_deputy_loses_access_immediately(h, pool):
    await make_deputy(h, pool)
    async with pool.acquire() as c:
        uid = await c.fetchval("SELECT user_id FROM deputies WHERE revoked_at IS NULL")
    h.clear()
    await h.press(ADMIN_ID, "theadmin", f"dep:revoke:{uid}")
    assert any("сняты" in t for t in h.texts())
    h.clear()
    await h.say(*DEP, "/start")
    assert "🛠 Админ-панель" not in h.buttons()
    await h.press(*DEP, "adm")
    assert "Админ-панель" not in " ".join(h.texts())


# ------------------------------------------------------------------ deputy has broad staff rights
async def test_deputy_can_manage_teams_matches_bracket_and_registration(h, pool):
    await make_deputy(h, pool)
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    h.clear()
    await h.press(*DEP, "adm:t:0")
    assert "Команды" in h.last()
    await h.press(*DEP, "adm:reg:set:0")
    assert not await h.svc.reg.is_open()
    await h.press(*DEP, "mt:new")
    await h.press(*DEP, f"mt:a:pick:{a}")
    await h.press(*DEP, f"mt:b:pick:{b}")
    await h.press(*DEP, "mt:stage:final")
    await h.press(*DEP, "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    for i in range(3):
        await h.press(*DEP, f"mt:gnew:{mid}")
        await h.press(*DEP, f"mt:gw:{mid}:{a}")
        await h.press(*DEP, f"mt:gok:{mid}:{a}:{i + 1}")
    assert "МАТЧ ЗАВЕРШЁН" in h.last()


# ------------------------------------------------------------------ BO5 UI, correcting a result
async def test_bo5_ui_enter_and_correct_result(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    A = ADMIN_ID
    await h.press(A, "adm", "mt:new")
    await h.press(A, "adm", f"mt:a:pick:{a}")
    await h.press(A, "adm", f"mt:b:pick:{b}")
    await h.press(A, "adm", "mt:stage:r8")
    await h.press(A, "adm", "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    await h.press(A, "adm", f"mt:gnew:{mid}")
    assert "КАТКА №1" in h.last()
    await h.press(A, "adm", f"mt:gw:{mid}:{a}")
    assert "ПОДТВЕРДИТЕ РЕЗУЛЬТАТ" in h.last() and "Катка №1" in h.last()
    await h.press(A, "adm", f"mt:gok:{mid}:{a}:1")
    assert "Счёт серии: 1 : 0" in h.last()
    await h.press(A, "adm", f"mt:gnew:{mid}")
    await h.press(A, "adm", f"mt:gw:{mid}:{b}")
    await h.press(A, "adm", f"mt:gok:{mid}:{b}:2")
    assert "Счёт серии: 1 : 1" in h.last()
    # correct game 1 from A to B
    await h.press(A, "adm", f"mt:gedit:{mid}")
    assert "Исправить результат" in h.last()
    async with pool.acquire() as c:
        g1 = await c.fetchval("SELECT id FROM match_games WHERE match_id=$1 AND game_number=1", mid)
    await h.press(A, "adm", f"mt:gec:{g1}")
    assert "Катка №1" in h.last()
    await h.press(A, "adm", f"mt:gew:{g1}:{b}")
    assert "Изменить победителя катки №1" in h.last()
    await h.press(A, "adm", f"mt:gewok:{g1}:{b}")
    assert "Результат обновлён" in h.last() and "Счёт серии: 0 : 2" in h.last()


async def test_bo5_double_click_confirm_records_one_game(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "mt:new")
    await h.press(ADMIN_ID, "adm", f"mt:a:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"mt:b:pick:{b}")
    await h.press(ADMIN_ID, "adm", "mt:stage:r8")
    await h.press(ADMIN_ID, "adm", "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    await asyncio.gather(*[h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{a}:1") for _ in range(6)])
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM match_games WHERE match_id=$1", mid) == 1


async def test_forged_game_callbacks(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    c = await make_team(pool, 3, "Gamma")
    await h.press(ADMIN_ID, "adm", "mt:new")
    await h.press(ADMIN_ID, "adm", f"mt:a:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"mt:b:pick:{b}")
    await h.press(ADMIN_ID, "adm", "mt:stage:r8")
    await h.press(ADMIN_ID, "adm", "mt:ok")
    async with pool.acquire() as conn:
        mid = await conn.fetchval("SELECT id FROM matches")
    await h.press(ADMIN_ID, "adm", f"mt:gw:{mid}:{c}")  # team not in this series
    assert "Некорректный выбор" in h.last()
    await h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{c}:1")  # forged confirm for a non-participant
    assert "Некорректный" in h.last() or "должен быть" in h.last() or "команд" in h.last()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM match_games WHERE match_id=$1", mid) == 0
    for data in (f"mt:gec:{'9' * 15}", f"mt:gew:{'9' * 15}:{a}", f"mt:gewok:{'9' * 15}:{a}", "mt:gnew:999999999"):
        await h.press(ADMIN_ID, "adm", data)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM match_games WHERE match_id=$1", mid) == 0


async def test_finish_with_full_podium_offers_final_broadcast(h, pool):
    a, b, c, d = [await make_team(pool, i, f"Team {chr(64 + i)}") for i in (1, 2, 3, 4)]

    async def create_match(stage, x, y):
        await h.press(ADMIN_ID, "adm", "mt:new")
        await h.press(ADMIN_ID, "adm", f"mt:a:pick:{x}")
        await h.press(ADMIN_ID, "adm", f"mt:b:pick:{y}")
        await h.press(ADMIN_ID, "adm", f"mt:stage:{stage}")
        await h.press(ADMIN_ID, "adm", "mt:ok")
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT id FROM matches ORDER BY id DESC LIMIT 1")

    async def win(mid, winner):
        for i in range(3):
            await h.press(ADMIN_ID, "adm", f"mt:gnew:{mid}")
            await h.press(ADMIN_ID, "adm", f"mt:gw:{mid}:{winner}")
            await h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{winner}:{i + 1}")

    s1 = await create_match("semi", a, b)
    await win(s1, a)
    s2 = await create_match("semi", c, d)
    await win(s2, d)
    third = await create_match("third", b, c)
    await win(third, b)
    final = await create_match("final", a, d)
    await win(final, a)
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:start")
    await h.press(ADMIN_ID, "adm", "tf:go")
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    await h.press(ADMIN_ID, "adm", "tf:ok")
    offer = next((t for t in h.texts() if "ИТОГИ ТУРНИРА" in t), None)
    assert offer is not None and "Team A" in offer
    assert {"👑 1 месту", "🥈 2 месту", "🥉 3 месту", "🎮 Остальным участникам", "👥 Всем участникам"} <= set(h.buttons())


# ------------------------------------------------------------------ history browsing
async def _finished_tournament(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "mt:new")
    await h.press(ADMIN_ID, "adm", f"mt:a:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"mt:b:pick:{b}")
    await h.press(ADMIN_ID, "adm", "mt:stage:final")
    await h.press(ADMIN_ID, "adm", "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    for i in range(3):
        await h.press(ADMIN_ID, "adm", f"mt:gnew:{mid}")
        await h.press(ADMIN_ID, "adm", f"mt:gw:{mid}:{a}")
        await h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{a}:{i + 1}")
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:start")
    await h.press(ADMIN_ID, "adm", "tf:early")  # third place was never created: early close
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    await h.press(ADMIN_ID, "adm", "tf:ok")
    return a, b


async def test_finish_tournament_requires_exact_confirmation_word(h, pool):
    await make_team(pool, 1, "Alpha")
    await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "tf:start")
    assert "не все призовые места" in h.last().lower() or "ТУРНИР ЕЩЁ НЕ ЗАВЕРШЁН" in h.last()
    await h.press(ADMIN_ID, "adm", "tf:early")
    await h.say(ADMIN_ID, "adm", "неверное слово")
    assert "Введите точно" in h.last()
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:ok")  # button not live yet (wrong state)
    assert "устарел" in (h.answers()[-1].text or "")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments WHERE status='active'") == 1
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    assert "Подтверждение получено" in h.last()
    await h.press(ADMIN_ID, "adm", "tf:ok")
    assert "ТУРНИР ЗАВЕРШЁН" in h.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments") == 2


async def test_finish_flow_end_to_end_and_final_broadcast_offer(h, pool):
    await make_team(pool, 1, "Alpha")
    other = await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "mt:new")
    async with pool.acquire() as c:
        a = await c.fetchval("SELECT id FROM teams WHERE name='Alpha'")
    await h.press(ADMIN_ID, "adm", f"mt:a:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"mt:b:pick:{other}")
    await h.press(ADMIN_ID, "adm", "mt:stage:final")
    await h.press(ADMIN_ID, "adm", "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    for i in range(3):
        await h.press(ADMIN_ID, "adm", f"mt:gnew:{mid}")
        await h.press(ADMIN_ID, "adm", f"mt:gw:{mid}:{a}")
        await h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{a}:{i + 1}")
    await h.press(ADMIN_ID, "adm", "tf:start")
    assert "не все призовые" in h.last().lower() or "не определено" in h.last()  # no third place match
    await h.press(ADMIN_ID, "adm", "tf:early")
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:ok")
    assert "ТУРНИР ЗАВЕРШЁН" in h.last()
    assert not any("ИТОГИ ТУРНИРА" in t for t in h.texts())  # early close: placements incomplete, offer suppressed
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams WHERE tournament_id = (SELECT id FROM tournaments WHERE status='active')") == 0


async def test_finish_denied_to_ordinary_user(h, pool):
    await make_team(pool, 1, "Alpha")
    before = await counts(pool)
    await h.press(5, "newuser1", "tf:start")
    await h.press(5, "newuser1", "tf:ok")
    assert await counts(pool) == before
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM tournaments") == 1


async def test_history_browsing_after_finish(h, pool):
    a, b = await _finished_tournament(h, pool)
    h.clear()
    await h.press(ADMIN_ID, "adm", "hist:0")
    assert "История турниров" in h.last()
    assert "🏆" in " ".join(h.buttons())
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM tournaments WHERE status <> 'active'")
    await h.press(ADMIN_ID, "adm", f"hist:c:{tid}")
    assert "ИСТОРИЯ ТУРНИРА" in h.last() and "Alpha" in h.last()
    h.clear()
    await h.press(ADMIN_ID, "adm", f"hist:t:{tid}")
    assert "Alpha" in h.buttons() and "Beta" in h.buttons()
    async with pool.acquire() as c:
        team_id = await c.fetchval("SELECT id FROM teams WHERE name='Alpha' AND tournament_id=$1", tid)
    h.clear()
    await h.press(ADMIN_ID, "adm", f"hist:tc:{team_id}:{tid}")
    assert "Alpha" in h.last() and ("1 место" in h.last() or "Чемпион" in h.last())
    h.clear()
    await h.press(ADMIN_ID, "adm", f"hist:b:{tid}")
    assert "Финал" in h.last()
    h.clear()
    await h.press(ADMIN_ID, "adm", f"hist:m:{tid}")
    assert "МАТЧИ" in h.last()
    h.clear()
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches WHERE tournament_id=$1", tid)
    await h.press(ADMIN_ID, "adm", f"hist:mc:{mid}:{tid}")
    assert "Катки" in h.last() and "1. 🏆" in h.last() and "Победитель серии" in h.last()
    h.clear()
    await h.press(ADMIN_ID, "adm", f"hist:stat:{tid}")
    assert "СТАТИСТИКА" in h.last() and "Alpha" in h.last()
    h.clear()
    await h.press(ADMIN_ID, "adm", f"hist:s:{tid}")
    assert "Соло" in h.last() or "Пусто" in h.last()


async def test_history_denied_to_ordinary_user(h, pool):
    a, b = await _finished_tournament(h, pool)
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM tournaments WHERE status <> 'active'")
    h.clear()
    for data in ("hist:0", f"hist:c:{tid}", f"hist:b:{tid}", f"hist:t:{tid}"):
        await h.press(5, "newuser1", data)
    assert h.texts() == []


# ------------------------------------------------------------------ result-notification composer (sn:)
async def test_series_custom_notification_flow(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "mt:new")
    await h.press(ADMIN_ID, "adm", f"mt:a:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"mt:b:pick:{b}")
    await h.press(ADMIN_ID, "adm", "mt:stage:r8")
    await h.press(ADMIN_ID, "adm", "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    for i in range(3):
        await h.press(ADMIN_ID, "adm", f"mt:gnew:{mid}")
        await h.press(ADMIN_ID, "adm", f"mt:gw:{mid}:{a}")
        await h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{a}:{i + 1}")
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:pick:{mid}")
    assert {"🏆 Победителям", "❌ Проигравшим"} <= set(h.buttons())
    await h.press(ADMIN_ID, "adm", f"sn:aud:series_winners:{mid}")
    await h.say(ADMIN_ID, "adm", "Отличная игра, чемпионы!")
    assert "ПРЕДПРОСМОТР" in " ".join(h.texts()) and "Получателей: <b>5</b>" in " ".join(h.texts())
    async with pool.acquire() as c:
        rid = await c.fetchval("SELECT id FROM result_broadcasts")
    h.clear()
    await asyncio.gather(*[h.press(ADMIN_ID, "adm", f"sn:send:{rid}") for _ in range(5)])
    await asyncio.gather(*h.svc.result_notify._tasks)
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT status, sent FROM result_broadcasts WHERE id=$1", rid)
    assert row["status"] == "done" and row["sent"] == 5
    delivered = [m for m in h.tg.requests if getattr(m, "text", None) == "Отличная игра, чемпионы!"]
    assert len(delivered) == 5  # exactly once each, no duplicates from the 5 concurrent send clicks


async def test_final_placement_broadcast_flow(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    await h.press(ADMIN_ID, "adm", "mt:new")
    await h.press(ADMIN_ID, "adm", f"mt:a:pick:{a}")
    await h.press(ADMIN_ID, "adm", f"mt:b:pick:{b}")
    await h.press(ADMIN_ID, "adm", "mt:stage:final")
    await h.press(ADMIN_ID, "adm", "mt:ok")
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    for i in range(3):
        await h.press(ADMIN_ID, "adm", f"mt:gnew:{mid}")
        await h.press(ADMIN_ID, "adm", f"mt:gw:{mid}:{a}")
        await h.press(ADMIN_ID, "adm", f"mt:gok:{mid}:{a}:{i + 1}")
    await h.press(ADMIN_ID, "adm", "tf:start")
    await h.press(ADMIN_ID, "adm", "tf:early")
    await h.say(ADMIN_ID, "adm", "ЗАВЕРШИТЬ")
    h.clear()
    await h.press(ADMIN_ID, "adm", "tf:ok")
    # even on early close, first/second ARE known (only third is missing) - but the offer is only
    # shown when the FULL podium is ready per _placements_text/p.ready; here we drive sn: directly
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM tournaments WHERE status <> 'active'")
    h.clear()
    await h.press(ADMIN_ID, "adm", f"sn:aud:final_first:{tid}")
    await h.say(ADMIN_ID, "adm", "Поздравляем чемпионов!")
    assert "Получателей: <b>5</b>" in " ".join(h.texts())
    async with pool.acquire() as c:
        rid = await c.fetchval("SELECT id FROM result_broadcasts")
    await h.press(ADMIN_ID, "adm", f"sn:send:{rid}")
    await asyncio.gather(*h.svc.result_notify._tasks)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT sent FROM result_broadcasts WHERE id=$1", rid) == 5


async def test_sn_denied_to_ordinary_user(h, pool):
    await make_team(pool, 1, "Alpha")
    await make_team(pool, 2, "Beta")
    before = await counts(pool)
    for data in ("sn:pick:1", "sn:aud:series_winners:1"):
        await h.press(5, "newuser1", data)
    assert h.texts() == []
    assert await counts(pool) == before


async def test_bracket_still_shows_after_new_tournament_started(h, pool):
    await _finished_tournament(h, pool)
    h.clear()
    await h.press(5, "newuser1", "bracket")
    assert "пока не созданы" in h.last()  # the new active tournament's bracket is empty
