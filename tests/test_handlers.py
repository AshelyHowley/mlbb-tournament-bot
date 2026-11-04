"""End-to-end handler/FSM tests through the real Dispatcher with a fake Telegram transport."""
import asyncio

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest

from app.services.registration import TgUser
from tests.harness import ADMIN_ID, Harness
from tests.helpers import dummy_method, make_team

pytestmark = pytest.mark.db

CAP = (1, "CaptainOne")


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


async def counts(pool):
    async with pool.acquire() as c:
        return (await c.fetchval("SELECT count(*) FROM teams"), await c.fetchval("SELECT count(*) FROM solo_players"),
                await c.fetchval("SELECT count(*) FROM matches"))


async def create_team_flow(h, uid=1, uname_="CaptainOne", name="Alpha Team", role="captain"):
    """reg:start -> team -> name -> creator role; ends on the confirmation screen."""
    await h.press(uid, uname_, "reg:start")
    await h.press(uid, uname_, "reg:team")
    await h.say(uid, uname_, name)
    await h.press(uid, uname_, f"reg:trole:{role}")

# ------------------------------------------------------------------ menu
async def test_start_menu_for_new_registered_and_admin(h, pool):
    await h.say(5, "newuser1", "/start")
    b = h.buttons()
    assert "🏆 Регистрация" in b and "🏆 Турнирная сетка" in b and "📢 Канал" in b and "💬 Чат" in b
    assert "👤 Моя регистрация" not in b and "🛠 Админ-панель" not in b
    await make_team(pool, 1)
    h.clear()
    await h.say(10, "cap0001", "/start")  # captain of team 1
    b = h.buttons()
    assert "👤 Моя регистрация" in b and "🏆 Регистрация" not in b
    h.clear()
    await h.say(ADMIN_ID, "theadmin", "/start")
    assert "🛠 Админ-панель" in h.buttons()


async def test_repeated_start_does_not_duplicate(h, pool):
    for _ in range(3):
        await h.say(CAP[0], CAP[1], "/start")
    assert await counts(pool) == (0, 0, 0)


# ------------------------------------------------------------------ team creation (invite-link model)
async def test_team_creation_flow_shows_link_and_stale_confirm(h, pool):
    await create_team_flow(h)
    assert "Проверьте данные" in h.last() and "Alpha Team" in h.last() and "1/5" in h.last()
    await h.press(*CAP, "reg:confirm")
    assert "Команда создана" in h.last() and "1/5" in h.last() and "Пригласите ещё 4" in h.last()
    assert "https://t.me/Test_Tournament_bot?start=team_" in h.last()
    assert {"👥 Моя команда", "🔗 Пригласить игрока"} <= set(h.buttons())
    assert await counts(pool) == (1, 0, 0)
    h.clear()
    await h.press(*CAP, "reg:confirm")  # stale second press
    assert await counts(pool) == (1, 0, 0)
    assert all(a.text and "устарела" in a.text for a in h.answers())
    h.clear()
    await h.press(*CAP, "me")
    assert "Alpha Team" in h.last() and "1/5" in h.last() and "👑" in h.last()


async def test_old_username_collection_is_gone(h, pool):
    await h.press(*CAP, "reg:team")
    await h.say(*CAP, "Alpha Team")
    await h.say(*CAP, "@p2user")  # role-choice state: free text is not consumed as a teammate
    assert "кнопки" in h.last()
    assert await counts(pool) == (0, 0, 0)


async def test_double_tap_confirm_concurrently(h, pool):
    await create_team_flow(h)
    await asyncio.gather(*[h.press(*CAP, "reg:confirm") for _ in range(10)])
    assert await counts(pool) == (1, 0, 0)


async def test_subscription_gate_on_create(h, pool):
    await create_team_flow(h)
    h.tg.chat_state["@yuliataetae"] = "left"
    h.clear()
    await h.press(*CAP, "reg:confirm")
    assert await counts(pool) == (0, 0, 0)
    assert {"📢 Подписаться на канал", "💬 Вступить в чат", "🔄 Проверить подписку"} <= set(h.buttons())
    h.tg.chat_state["@yuliataetae"] = "member"
    await h.press(*CAP, "sub:check")
    assert await counts(pool) == (1, 0, 0)


async def test_telegram_api_error_does_not_create_team(h, pool):
    await create_team_flow(h)
    h.tg.chat_state["@MLBangBangTournamentChat"] = TelegramBadRequest(dummy_method(), "chat not found")
    h.clear()
    await h.press(*CAP, "reg:confirm")
    assert await counts(pool) == (0, 0, 0)
    assert "Не удалось проверить" in h.last()


async def test_team_name_validation_in_flow(h, pool):
    await h.press(*CAP, "reg:team")
    await h.say(*CAP, "A")
    assert "от 2 до 40" in h.last()
    await h.say(*CAP, "x" * 500)
    assert "от 2 до 40" in h.last()
    await h.say(*CAP, "Good Team")
    assert "Ваша роль в этой команде" in h.last()
    assert await counts(pool) == (0, 0, 0)


async def test_back_and_cancel_navigation(h, pool):
    await h.press(*CAP, "reg:team")
    await h.say(*CAP, "Nav Team")
    await h.press(*CAP, "reg:trole:member")
    assert "Проверьте данные" in h.last()
    await h.press(*CAP, "reg:back")  # confirm -> role
    assert "Ваша роль в этой команде" in h.last()
    await h.press(*CAP, "reg:back")  # role -> name
    assert "название команды" in h.last()
    await h.say(*CAP, "Nav Team 2")
    await h.press(*CAP, "reg:cancel")
    assert "Турнир Mobile Legends" in h.last()
    h.clear()
    await h.press(*CAP, "reg:cancel")  # after cancel: state is gone -> stale
    assert all("устарела" in (a.text or "") for a in h.answers())
    assert await counts(pool) == (0, 0, 0)


async def test_duplicate_name_detected_early(h, pool):
    await make_team(pool, 1, "Alpha Team")
    await h.press(50, "otherguy1", "reg:team")
    await h.say(50, "otherguy1", "ALPHA team")
    assert "уже зарегистрирована" in h.last()


async def test_user_without_username_can_register_team_and_solo(h, pool):
    await create_team_flow(h, 60, None, "Nameless")
    await h.press(60, None, "reg:confirm")
    assert "Команда создана" in h.last() and "None" not in h.last()
    await h.press(61, None, "reg:solo")
    await h.press(61, None, "reg:role:MID")
    assert "None" not in h.last()
    await h.press(61, None, "reg:confirm")
    assert await counts(pool) == (1, 1, 0)


async def test_html_injection_is_escaped(h, pool):
    await h.press(*CAP, "reg:team")
    await h.say(*CAP, "<b>Boom</b> & <i>x</i>")
    assert "<b>Boom</b>" not in h.last() and "&lt;b&gt;Boom&lt;/b&gt;" in h.last()

# ------------------------------------------------------------------ solo
async def test_solo_flow(h, pool):
    await h.press(7, "sologuy01", "reg:start")
    await h.press(7, "sologuy01", "reg:solo")
    await h.press(7, "sologuy01", "reg:role:JUNGLE")
    assert "JUNGLE" in h.last() and "@sologuy01" in h.last()
    await h.press(7, "sologuy01", "reg:confirm")
    assert await counts(pool) == (0, 1, 0)
    h.clear()
    await h.press(7, "sologuy01", "me")
    assert "JUNGLE" in h.last() and "Роль" in h.last()
    h.clear()
    await h.press(7, "sologuy01", "reg:start")  # already registered
    assert "уже зарегистрированы" in h.last()


async def test_forged_role_rejected(h, pool):
    await h.press(7, "sologuy01", "reg:solo")
    await h.press(7, "sologuy01", "reg:role:HACKER")
    await h.press(7, "sologuy01", "reg:confirm")
    assert await counts(pool) == (0, 0, 0)


async def test_registration_closed_ui(h, pool):
    await h.svc.reg.set_open(False)
    await h.press(*CAP, "reg:start")
    assert "закрыта" in h.last()
    await h.press(*CAP, "reg:team")
    assert "закрыта" in h.last()


# ------------------------------------------------------------------ admin authorisation
ADMIN_CALLBACKS = ["adm", "adm:t:0", "adm:tc:1", "adm:td:1", "adm:tdok:1", "adm:s:0", "adm:sdok:10", "adm:reg:set:0",
                   "adm:m", "mt:new", "mt:ok", "mt:wok:1:1", "mt:dok:1", "adm:bc", "bc:aud:all", "bc:send:1", "adm:reg"]


@pytest.mark.parametrize("data", ADMIN_CALLBACKS)
async def test_non_admin_cannot_use_admin_callbacks(h, pool, data):
    await make_team(pool, 1)
    await h.svc.reg.register_solo(TgUser(10 + 1000, "soloxx01"), "EXP")
    before = await counts(pool)
    h.clear()
    await h.press(123, "hacker001", data)
    assert await counts(pool) == before
    assert h.texts() == [] and all("устарела" in (a.text or "") for a in h.answers())
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT value FROM settings WHERE key='registration_open'") == "true"


async def test_non_admin_admin_command_and_text_states(h, pool):
    h.clear()
    await h.say(123, "hacker001", "/admin")
    assert "Админ-панель" not in "".join(h.texts())
    await h.say(123, "hacker001", "some broadcast text")
    assert await counts(pool) == (0, 0, 0)


@pytest.mark.parametrize("data", ["adm:tc:abc", "adm:tc:" + "9" * 40, "mt:w:1", "mt:w:a:b", "bc:send:x", "reg:role:", "adm:t:-1",
                                  "adm:tc:0", "mt:l:bogus:0", "", "x" * 64, "adm:reg:set:2", "mt:stage:../etc"])
async def test_malformed_callbacks_are_harmless(h, pool, data):
    await h.press(ADMIN_ID, "theadmin", data)
    await h.press(5, "newuser1", data)
    assert h.answers()  # every callback gets answered


async def test_admin_teams_list_card_and_confirmed_delete(h, pool):
    tid = await make_team(pool, 1, "Alpha")
    h.clear()
    await h.press(ADMIN_ID, "theadmin", "adm:t:0")
    assert "Всего: <b>1</b>" in h.last() and "Alpha" in h.buttons()
    await h.press(ADMIN_ID, "theadmin", f"adm:tc:{tid}")
    assert "cap0001" in h.last() and "mem0011" in h.last() and "Дата регистрации" in h.last()
    await h.press(ADMIN_ID, "theadmin", f"adm:td:{tid}")
    assert "⚠️ Подтвердить удаление" in h.buttons() and "❌ Отмена" in h.buttons()
    assert (await counts(pool))[0] == 1  # nothing deleted yet
    await h.press(ADMIN_ID, "theadmin", f"adm:tc:{tid}")  # cancel path
    assert (await counts(pool))[0] == 1
    await h.press(ADMIN_ID, "theadmin", f"adm:tdok:{tid}")
    assert (await counts(pool))[0] == 0
    await h.press(ADMIN_ID, "theadmin", f"adm:tdok:{tid}")  # double click -> graceful
    assert "не найдена" in h.last()


async def test_admin_registration_toggle_idempotent(h, pool):
    await h.press(ADMIN_ID, "theadmin", "adm:reg:set:0")
    await h.press(ADMIN_ID, "theadmin", "adm:reg:set:0")
    assert not await h.svc.reg.is_open()
    await h.press(ADMIN_ID, "theadmin", "adm:reg:set:1")
    assert await h.svc.reg.is_open()


# ------------------------------------------------------------------ matches via UI
async def test_match_flow_and_bo5_result(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    A = ADMIN_ID
    await h.press(A, "adm", "mt:new")
    await h.press(A, "adm", f"mt:a:pick:{a}")
    await h.press(A, "adm", f"mt:b:pick:{a}")          # same team -> alert, still in team_b
    assert any(x.show_alert for x in h.answers())
    await h.press(A, "adm", f"mt:b:pick:{b}")
    await h.press(A, "adm", "mt:stage:semi")
    assert "Полуфинал" in h.last() and "Alpha" in h.last() and "Beta" in h.last()
    await asyncio.gather(*[h.press(A, "adm", "mt:ok") for _ in range(8)])  # double click
    assert (await counts(pool))[2] == 1
    async with pool.acquire() as c:
        mid = await c.fetchval("SELECT id FROM matches")
    # three games, Alpha wins 3:0
    for i in range(3):
        await h.press(A, "adm", f"mt:gnew:{mid}")
        await h.press(A, "adm", f"mt:gw:{mid}:{a}")
        await h.press(A, "adm", f"mt:gok:{mid}:{a}:{i + 1}")
    assert "МАТЧ ЗАВЕРШЁН" in h.last() and "Победитель серии" in h.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT winner_id FROM matches") == a
        assert await c.fetchval("SELECT count(*) FROM match_games WHERE match_id = $1", mid) == 3
    # a 4th game is refused (series already decided)
    await h.press(A, "adm", f"mt:gnew:{mid}")
    assert "уже завершена" in h.last()
    h.clear()
    await h.press(5, "newuser1", "bracket")  # public bracket
    text = h.last()
    assert "Полуфинал" in text and "Alpha" in text and "Победитель" in text


async def test_bracket_visible_without_admin_rights_when_empty(h):
    await h.press(5, "newuser1", "bracket")
    assert "пока не созданы" in h.last()


async def test_match_needs_two_teams(h, pool):
    await make_team(pool, 1)
    await h.press(ADMIN_ID, "adm", "mt:new")
    assert "минимум 2" in h.last()


# ------------------------------------------------------------------ broadcast via UI
async def test_broadcast_flow(h, pool):
    await make_team(pool, 1)
    await h.svc.reg.register_solo(TgUser(77, "sologuy01"), "EXP")
    A = ADMIN_ID
    await h.press(A, "adm", "adm:bc")
    await h.press(A, "adm", "bc:aud:solo")
    await h.say(A, "adm", "<b>Привет</b> всем")
    text = " ".join(h.texts())
    assert "Предпросмотр" in text and "Количество: <b>1</b>" in text
    assert {"📤 Отправить", "✏️ Изменить", "❌ Отмена"} <= set(h.buttons())
    async with pool.acquire() as c:
        bid = await c.fetchval("SELECT id FROM broadcasts")
    h.clear()
    await asyncio.gather(*[h.press(A, "adm", f"bc:send:{bid}") for _ in range(6)])
    await asyncio.gather(*h.svc.bc._tasks)
    sent = [m for m in h.tg.requests if getattr(m, "chat_id", None) == 77]
    assert len(sent) == 1 and sent[0].text == "<b>Привет</b> всем" and sent[0].parse_mode is None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM broadcasts") == 1
        assert await c.fetchval("SELECT status FROM broadcasts") == "done"


async def test_broadcast_edit_and_cancel(h, pool):
    A = ADMIN_ID
    await h.press(A, "adm", "bc:aud:all")
    await h.say(A, "adm", "first")
    async with pool.acquire() as c:
        bid = await c.fetchval("SELECT id FROM broadcasts")
    await h.press(A, "adm", f"bc:edit:{bid}")
    await h.say(A, "adm", "second")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM broadcasts WHERE id=$1", bid) == "cancelled"
        bid2 = await c.fetchval("SELECT max(id) FROM broadcasts")
    await h.press(A, "adm", f"bc:cancel:{bid2}")
    await h.press(A, "adm", f"bc:send:{bid2}")  # stale after cancel
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM broadcasts WHERE status='sending' OR status='done'") == 0




# ------------------------------------------------------------------ regressions for audit findings
async def test_regression_blocked_user_reset_by_start_within_cache_window(h, pool):
    await h.say(5, "newuser1", "/start")            # populates the tracking cache
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET is_blocked = TRUE WHERE telegram_id = 5")
    await h.say(5, "newuser1", "/start")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT is_blocked FROM users WHERE telegram_id = 5") is False


async def test_regression_broadcast_with_no_recipients_cannot_be_sent(h, pool):
    await h.press(ADMIN_ID, "adm", "bc:aud:solo")
    await h.say(ADMIN_ID, "adm", "hello")
    assert "нет доступных получателей" in h.last()
    assert "📤 Отправить" not in h.buttons()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM broadcasts") == "cancelled"


async def test_regression_delete_team_with_matches_shows_message_not_error(h, pool):
    a, b = await make_team(pool, 1, "Alpha"), await make_team(pool, 2, "Beta")
    await h.svc.admin.create_match(a, b, "r8")
    await h.press(ADMIN_ID, "adm", f"adm:tdok:{a}")
    assert "участвует в матчах" in h.last()
    assert (await counts(pool))[0] == 2
