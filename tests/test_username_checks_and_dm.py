"""Username gate on invites, admin participant checks (username / channel+chat) and personal captain DMs."""
import pytest
import pytest_asyncio
from aiogram.methods import SendMessage

from tests.harness import ADMIN_ID, Harness
from tests.helpers import make_team, team_token

pytestmark = pytest.mark.db
ADM = (ADMIN_ID, "admin")


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


def sent_to(h, uid):
    return [m for m in h.tg.requests if isinstance(m, SendMessage) and m.chat_id == uid]


async def members(pool):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT count(*) FROM team_members")


async def test_join_without_username_is_blocked(h, pool):
    tid = await make_team(pool, 1, size=1)
    tok = await team_token(pool, tid)
    await h.join(50, None, tok)
    assert "Нужен username" in h.last() and await members(pool) == 1
    await h.join(50, "joiner50", tok)
    assert await members(pool) == 2


async def test_username_check_notifies_only_real_offenders(h, pool):
    await make_team(pool, 1, size=3)  # ids 10 (captain), 11, 12
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET username = NULL, username_norm = NULL WHERE telegram_id IN (11, 12)")
    h.tg.usernames[12] = "fixed12"  # set it later in Telegram, the DB copy is stale
    h.clear()
    await h.press(*ADM, "chk:uname")
    assert "Без username: <b>1</b>" in h.last()
    assert any("Нужен username" in m.text for m in sent_to(h, 11))
    assert not sent_to(h, 12)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT username FROM users WHERE telegram_id = 12") == "fixed12"


async def test_subscription_check_lists_and_reminds(h, pool):
    await make_team(pool, 1, size=2)  # 10, 11
    h.tg.chat_state[(h.cfg.chat_id, 11)] = "left"
    h.clear()
    await h.press(*ADM, "chk:sub")
    rep = h.last()
    assert "Не выполняют условия: <b>1</b>" in rep and "нет: чат" in rep
    assert not sent_to(h, 11)  # the check alone does not message anyone
    assert "📨 Напомнить нарушителям" in h.buttons()
    h.clear()
    await h.press(*ADM, "chk:subnotify")
    assert any("Условия участия" in m.text for m in sent_to(h, 11)) and not sent_to(h, 10)


async def test_checks_are_staff_only(h, pool):
    await make_team(pool, 1, size=1)
    h.clear()
    await h.press(10, "cap0001", "chk:uname")
    assert "Проверено" not in " ".join(h.texts())


async def test_personal_message_to_captain_and_reply(h, pool):
    await make_team(pool, 1, size=2)  # captain 10
    await h.press(*ADM, "dm:list:0")
    assert any("Team 1" in b for b in h.buttons())
    await h.press(*ADM, "dm:to:10")
    h.clear()
    await h.say(*ADM, "Привет, капитан <b>")
    to_cap = sent_to(h, 10)
    assert "Сообщение от организатора" in to_cap[0].text and to_cap[1].text == "Привет, капитан <b>"
    assert to_cap[1].parse_mode is None
    assert to_cap[1].reply_markup.inline_keyboard[0][0].callback_data == f"dmr:{ADMIN_ID}"
    assert "доставлено" in h.last()
    # captain answers
    await h.press(10, "cap0001", f"dmr:{ADMIN_ID}")
    h.clear()
    await h.say(10, "cap0001", "Ответ капитана")
    to_admin = sent_to(h, ADMIN_ID)
    assert "Ответ от" in to_admin[0].text and "Team 1" in to_admin[0].text and to_admin[1].text == "Ответ капитана"
    assert to_admin[1].reply_markup.inline_keyboard[0][0].callback_data == "dm:to:10"


async def test_forged_reply_target_goes_to_main_admin(h, pool):
    await make_team(pool, 1, size=1)
    await h.press(10, "cap0001", "dmr:11")  # 11 is not staff
    h.clear()
    await h.say(10, "cap0001", "hi")
    assert not sent_to(h, 11) and any(m.text == "hi" for m in sent_to(h, ADMIN_ID))


async def test_dm_to_non_captain_is_refused(h, pool):
    await make_team(pool, 1, size=2)
    await h.press(*ADM, "dm:to:11")  # a plain member
    assert "больше не капитан" in h.last()
