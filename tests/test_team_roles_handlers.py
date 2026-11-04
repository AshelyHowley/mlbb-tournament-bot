"""Role selection, captain invite, registrar tools, captain transfer and "my registration" through the real dispatcher."""
import asyncio
import re

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest

from tests.harness import ADMIN_ID, Harness
from tests.helpers import dummy_method, make_team, member_of

pytestmark = pytest.mark.db

R = (1, "regist01")      # registrar
C = (2, None)            # future captain, no username
P3 = (3, "player03")
P4 = (4, None)
LINK = re.compile(r"start=(team|captain)_([A-Za-z0-9_-]{32,48})")


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


async def counts(pool):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT count(*) FROM teams"), await c.fetchval("SELECT count(*) FROM team_members")


async def create_via_ui(h, who, role, name="Alpha Team"):
    await h.press(*who, "reg:start")
    await h.press(*who, "reg:team")
    await h.say(*who, name)
    await h.press(*who, f"reg:trole:{role}")
    await h.press(*who, "reg:confirm")
    return h.last()


def tokens(text):
    return {kind: tok for kind, tok in LINK.findall(text)}


async def captain_link_of(h, who=R):
    """The registrar opens 'captain link' and we read the token from the bot's message."""
    await h.press(*who, "team:cap")
    return tokens(h.last())["captain"]


# ------------------------------------------------------------------ role selection
async def test_role_prompt_after_name(h, pool):
    await h.press(*R, "reg:team")
    await h.say(*R, "Alpha Team")
    assert "Ваша роль в этой команде?" in h.last()
    assert {"👑 Я капитан", "🎮 Я участник, капитан другой", "📝 Я только регистрирую команду"} <= set(h.buttons())
    assert await counts(pool) == (0, 0)
    await h.say(*R, "@somebody")  # free text is not a role
    assert "кнопки" in h.last() and await counts(pool) == (0, 0)


async def test_creator_captain_gets_player_link(h, pool):
    text = await create_via_ui(h, R, "captain")
    t = tokens(text)
    assert set(t) == {"team"} and "1/5" in text and "Пригласите ещё 4" in text
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT captain_id, created_by FROM teams")
    assert (row["captain_id"], row["created_by"]) == (R[0], R[0])


async def test_creator_member_gets_captain_link_only(h, pool):
    text = await create_via_ui(h, R, "member")
    t = tokens(text)
    assert set(t) == {"captain"}, text                # NO player link while the captain is pending
    assert "Капитан: ожидается" in text and "Участников: 1/5" in text
    assert {"👥 Моя команда", "🔗 Ссылка капитана"} <= set(h.buttons())
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") is None
        assert await c.fetchval("SELECT is_captain FROM team_members") is False


async def test_registrar_only_takes_no_seat(h, pool):
    text = await create_via_ui(h, R, "registrar")
    assert set(tokens(text)) == {"captain"} and "Участников: 0/5" in text and "в состав не входите" in text
    assert await counts(pool) == (1, 0)


async def test_back_from_confirm_returns_to_role_choice(h, pool):
    await h.press(*R, "reg:team")
    await h.say(*R, "Alpha Team")
    await h.press(*R, "reg:trole:registrar")
    assert "Проверьте данные" in h.last() and "Только регистрирую" in h.last() and "0/5" in h.last()
    await h.press(*R, "reg:back")
    assert "Ваша роль в этой команде?" in h.last()
    await h.press(*R, "reg:back")
    assert "название команды" in h.last()


async def test_forged_role_callback_is_rejected(h, pool):
    await h.press(*R, "reg:team")
    await h.say(*R, "Alpha Team")
    for bad in ("reg:trole:owner", "reg:trole:", "reg:trole:captain;", "reg:trole:CAPTAIN"):
        await h.press(*R, bad)
    await h.press(*R, "reg:confirm")  # still in the role state: confirm is stale
    assert await counts(pool) == (0, 0)


async def test_second_registration_by_ordinary_user_is_refused_in_ui(h, pool):
    await create_via_ui(h, R, "registrar")
    await h.press(*R, "reg:team")
    assert "уже зарегистрировали команду" in h.last()
    assert await counts(pool) == (1, 0)


async def test_registrar_only_can_still_register_solo_and_menu_offers_both(h, pool):
    await create_via_ui(h, R, "registrar")
    h.clear()
    await h.say(*R, "/start")
    assert {"🏆 Регистрация", "👤 Моя регистрация", "👥 Моя команда"} <= set(h.buttons())
    await h.press(*R, "reg:solo")
    await h.press(*R, "reg:role:EXP")
    await h.press(*R, "reg:confirm")
    assert "Регистрация завершена" in " ".join(h.texts())
    h.clear()
    await h.press(*R, "me")
    t = h.last()
    assert "Регистратор" in t and "Роль: <b>EXP</b>" in t   # both registrations are shown


# ------------------------------------------------------------------ captain invite
async def test_captain_invite_flow_and_reuse(h, pool):
    await create_via_ui(h, R, "registrar")
    token = await captain_link_of(h)
    h.clear()
    await h.say(*C, f"/start captain_{token}")
    t = h.last()
    assert "Приглашение капитана" in t and "Alpha Team" in t and "Состав: 0/5" in t and "стать капитаном этой команды" in t
    assert {"✅ Стать капитаном", "❌ Отказаться"} <= set(h.buttons())
    assert await counts(pool) == (1, 0)                    # opening does not assign
    h.clear()
    await h.press(*C, f"cap:ok:{token}")
    assert "Вы стали капитаном команды" in " ".join(h.texts()) and "1/5" in " ".join(h.texts())
    assert any("Капитан назначен" in x for x in h.texts() if x)   # registrar was told
    assert await counts(pool) == (1, 1)
    h.clear()
    await h.say(*P3, f"/start captain_{token}")            # the link is dead now
    assert "уже назначен капитан" in h.last()
    await h.press(*P3, f"cap:ok:{token}")
    assert "уже назначен капитан" in h.last() and await counts(pool) == (1, 1)
    h.clear()
    await h.press(*C, f"cap:ok:{token}")                   # the captain double-clicking
    assert "уже капитан" in h.last() and await counts(pool) == (1, 1)


async def test_double_tap_on_accept_captain(h, pool):
    await create_via_ui(h, R, "registrar")
    token = await captain_link_of(h)
    await asyncio.gather(*[h.press(*C, f"cap:ok:{token}") for _ in range(8)])
    assert await counts(pool) == (1, 1)


async def test_player_link_is_closed_until_a_captain_exists(h, pool):
    await create_via_ui(h, R, "member")
    async with pool.acquire() as c:
        ptok = await c.fetchval("SELECT invite_token FROM teams")
    h.clear()
    await h.say(*P3, f"/start team_{ptok}")
    assert "Капитан этой команды пока не назначен" in h.last() and "✅ Вступить в команду" not in h.buttons()
    await h.press(*P3, f"join:ok:{ptok}")
    assert "Капитан этой команды пока не назначен" in h.last() and await counts(pool) == (1, 1)
    ctok = await captain_link_of(h)
    await h.press(*C, f"cap:ok:{ctok}")
    h.clear()
    await h.say(*P3, f"/start team_{ptok}")
    assert "Приглашение в команду" in h.last()
    await h.press(*P3, f"join:ok:{ptok}")
    assert await counts(pool) == (1, 3)


async def test_existing_member_takes_captaincy_via_link_no_duplicate(h, pool):
    await create_via_ui(h, R, "member")
    token = await captain_link_of(h)
    await h.press(*R, f"cap:ok:{token}")
    assert "Вы стали капитаном" in " ".join(h.texts()) and await counts(pool) == (1, 1)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == R[0]


@pytest.mark.parametrize("payload", ["captain_garbage", "captain_", "captain_" + "a" * 100, "CAPTAIN_" + "a" * 32,
                                     "captain_" + "a" * 31 + "'", "captain_../../x" + "a" * 20])
async def test_malformed_captain_payload_is_a_plain_start(h, pool, payload):
    await h.say(*C, f"/start {payload}")
    assert "Турнир Mobile Legends" in h.last() and "Приглашение" not in h.last()


async def test_unknown_and_swapped_tokens(h, pool):
    await create_via_ui(h, R, "registrar")
    async with pool.acquire() as c:
        ptok = await c.fetchval("SELECT invite_token FROM teams")
    for payload in (f"captain_{'A' * 32}", f"captain_{ptok}"):   # unknown / a PLAYER token used as captain token
        h.clear()
        await h.say(*C, f"/start {payload}")
        assert "больше недействительно" in h.last()
    await h.press(*C, f"cap:ok:{ptok}")
    assert "больше недействительно" in h.last() and await counts(pool) == (1, 0)


@pytest.mark.parametrize("data", ["cap:ok:", "cap:ok:short", "cap:ok:" + "a" * 49, "cap:ok:" + "a" * 31 + "!", "cap:", "cap:no:" + "a" * 32,
                                  "cap:ok:" + "%" * 32])
async def test_forged_captain_callbacks(h, pool, data):
    await create_via_ui(h, R, "registrar")
    await h.press(*C, data)
    assert await counts(pool) == (1, 0) and h.answers()


async def test_subscription_gate_on_captain_accept(h, pool):
    await create_via_ui(h, R, "registrar")
    token = await captain_link_of(h)
    h.tg.chat_state["@yuliataetae"] = "left"
    h.clear()
    await h.press(*C, f"cap:ok:{token}")
    assert await counts(pool) == (1, 0)
    assert {"📢 Подписаться на канал", "💬 Вступить в чат", "🔄 Проверить подписку"} <= set(h.buttons())
    h.tg.chat_state["@yuliataetae"] = "member"
    await h.press(*C, f"cap:ok:{token}")          # "Проверить подписку" retries the acceptance
    assert await counts(pool) == (1, 1)


async def test_telegram_error_during_subscription_check_blocks_captain(h, pool):
    await create_via_ui(h, R, "registrar")
    token = await captain_link_of(h)
    h.tg.chat_state["@yuliataetae"] = TelegramBadRequest(dummy_method(), "chat not found")
    await h.press(*C, f"cap:ok:{token}")
    assert await counts(pool) == (1, 0) and "Не удалось проверить" in h.last()


async def test_html_in_team_name_is_escaped_on_captain_invite(h, pool):
    await create_via_ui(h, R, "registrar", name="<b>X</b> & <i>y</i>")
    token = await captain_link_of(h)
    h.clear()
    await h.say(*C, f"/start captain_{token}")
    assert "<b>X</b>" not in h.last() and "&lt;b&gt;X&lt;/b&gt;" in h.last()


# ------------------------------------------------------------------ registrar tools
async def test_registrar_regenerates_captain_link(h, pool):
    await create_via_ui(h, R, "registrar")
    await h.press(*R, "team")
    assert {"🔗 Ссылка капитана", "🔄 Обновить ссылку капитана", "🗑 Отменить регистрацию"} <= set(h.buttons())
    old = await captain_link_of(h)
    h.clear()
    await h.press(*R, "team:capreset")
    new = tokens(h.last())["captain"]
    assert new != old and "Старая ссылка больше не работает" in h.last()
    h.clear()
    await h.say(*C, f"/start captain_{old}")
    assert "больше недействительно" in h.last()
    await h.press(*C, f"cap:ok:{old}")
    assert "больше недействительно" in h.last() and await counts(pool) == (1, 0)
    await h.press(*C, f"cap:ok:{new}")
    assert await counts(pool) == (1, 1)


async def test_registrar_cancels_registration_with_confirmation(h, pool):
    await create_via_ui(h, R, "member")
    await h.press(*R, "team:cancel")
    assert "Отменить регистрацию?" in h.last() and {"✅ Подтвердить", "❌ Отмена"} <= set(h.buttons())
    assert await counts(pool) == (1, 1)
    await h.press(*R, "team:cancelok")
    assert "отменена" in h.last() and await counts(pool) == (0, 0)
    await h.press(*R, "team:cancelok")                     # double click / stale
    assert await counts(pool) == (0, 0)


async def test_registrar_loses_tools_once_captain_assigned(h, pool):
    await create_via_ui(h, R, "registrar")
    await h.press(*C, f"cap:ok:{await captain_link_of(h)}")
    await h.press(*P3, f"join:ok:{await pool.fetchval('SELECT invite_token FROM teams')}")
    h.clear()
    await h.press(*R, "team")
    t = h.last()
    assert "Регистратор" in t and "вы не входите в игровой состав" in t and "Состав: 2/5" in t
    shown = set(h.buttons())
    assert shown.isdisjoint({"🔄 Обновить ссылку капитана", "🗑 Отменить регистрацию", "🔄 Управление составом",
                             "👑 Передать капитанство", "🚪 Покинуть команду", "🔗 Пригласить игрока"})
    # forged callbacks: the server, not the missing button, decides
    before = await counts(pool)
    for data in ("team:capreset", "team:cancelok", "team:cap", f"team:rmok:{P3[0]}", f"team:rpok:{P3[0]}", f"team:trok:{P3[0]}",
                 "team:mng", "team:inv", "team:leaveok"):
        await h.press(*R, data)
    assert await counts(pool) == before
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == C[0]


# ------------------------------------------------------------------ "my registration" for the three roles
async def test_my_registration_texts_for_each_role(h, pool):
    # captain
    await create_via_ui(h, (10, "cap0010"), "captain", name="Cap Team")
    h.clear()
    await h.press(10, "cap0010", "me")
    t = h.last()
    assert "Cap Team" in t and "👑 Ваша роль: Капитан" in t and "Состав: 1/5" in t and "Капитан:" not in t.replace("Ваша роль: Капитан", "")
    # member, captain pending
    await create_via_ui(h, (20, None), "member", name="Mem Team")
    h.clear()
    await h.press(20, None, "me")
    t = h.last()
    assert "🎮 Ваша роль: Участник" in t and "👑 Капитан: ожидается" in t and "Состав: 1/5" in t and "None" not in t
    # registrar only
    await create_via_ui(h, (30, "reg0030"), "registrar", name="Reg Team")
    h.clear()
    await h.press(30, "reg0030", "me")
    t = h.last()
    assert "📝 Ваша роль: Регистратор" in t and "👑 Капитан: ожидается" in t and "Состав: 0/5" in t
    # once the captain is assigned the registrar sees the name
    async with pool.acquire() as c:
        ctok = await c.fetchval("SELECT captain_invite_token FROM teams WHERE name='Reg Team'")
    await h.press(40, None, f"cap:ok:{ctok}")
    h.clear()
    await h.press(30, "reg0030", "me")
    assert "👑 Капитан: U40" in h.last() and "Состав: 1/5" in h.last()
    h.clear()
    await h.press(20, None, "me")
    assert "🎮 Ваша роль: Участник" in h.last()


# ------------------------------------------------------------------ captain transfer via UI
async def test_transfer_flow(h, pool):
    await make_team(pool, 1)                              # captain 10 'cap0001', members 11..14
    cap, new = (10, "cap0001"), member_of(1, 3)
    h.clear()
    await h.press(*cap, "team")
    assert "👑 Передать капитанство" in h.buttons()
    await h.press(*cap, "team:tr")
    listed = [b for b in h.buttons() if "Member" in b]
    assert len(listed) == 4 and not any("Captain" in b for b in h.buttons())
    await h.press(*cap, f"team:trc:{new.id}")
    assert "Передать капитанство?" in h.last() and "Новый капитан:" in h.last() and "Member 13" in h.last()
    assert {"✅ Подтвердить", "❌ Отмена"} <= set(h.buttons())
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == 10       # nothing yet
    h.clear()
    await asyncio.gather(*[h.press(*cap, f"team:trok:{new.id}") for _ in range(6)])   # double/multi tap
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == new.id
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE is_captain") == 1
    assert any("Капитанство передано" in x for x in h.texts())
    assert any("Вам передано капитанство" in x for x in h.texts())          # the new captain was told
    h.clear()
    await h.press(new.id, "mem0013", "team")
    assert "👑 Ваша роль: Капитан" in h.last() and "👑 Передать капитанство" in h.buttons()
    h.clear()
    await h.press(*cap, "team")
    assert "🎮 Ваша роль: Участник" in h.last() and "🚪 Покинуть команду" in h.buttons()
    assert "🔄 Управление составом" not in h.buttons()
    await h.press(*cap, f"team:trok:{member_of(1, 1).id}")       # the old captain replays a transfer: refused
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == new.id


async def test_transfer_forgeries(h, pool):
    await make_team(pool, 1)
    await make_team(pool, 2)
    cap = (10, "cap0001")
    async with pool.acquire() as c:
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (555,'Outsider')")
    h.clear()
    for target in (555, 21, 20, 10, 999999999):                   # outsider / other team's member / other captain / self / nonexistent
        await h.press(*cap, f"team:trc:{target}")
        await h.press(*cap, f"team:trok:{target}")
    for data in ("team:trok:abc", "team:trok:0", "team:trok:" + "9" * 40, "team:trc:", "team:tr:1"):
        await h.press(*cap, data)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams WHERE id = (SELECT team_id FROM team_members WHERE user_id = 10)") == 10
    # a plain member and the registrar of another team cannot transfer either
    await h.press(11, "mem0011", "team:trok:12")
    await h.press(*R, "team:trok:11")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams WHERE id = (SELECT team_id FROM team_members WHERE user_id = 10)") == 10
        assert await c.fetchval("SELECT count(*) FROM team_members") == 10


async def test_captain_alone_cannot_open_transfer(h, pool):
    await create_via_ui(h, (10, "cap0001"), "captain")
    await h.press(10, "cap0001", "team")
    assert "👑 Передать капитанство" not in h.buttons()
    await h.press(10, "cap0001", "team:tr")
    assert "нет других участников" in h.last()


async def test_replacement_still_works_after_captain_link_flow(h, pool):
    await create_via_ui(h, R, "registrar")
    await h.press(*C, f"cap:ok:{await captain_link_of(h)}")
    async with pool.acquire() as c:
        ptok = await c.fetchval("SELECT invite_token FROM teams")
    for who in (P3, P4, (5, None), (6, "player06")):
        await h.press(*who, f"join:ok:{ptok}")
    assert await counts(pool) == (1, 5)
    await h.press(*C, f"team:rpok:{P3[0]}")
    assert await counts(pool) == (1, 4)
    await h.press(7, None, f"join:ok:{ptok}")
    assert await counts(pool) == (1, 5)


async def test_admin_sees_pending_captain_and_registrar(h, pool):
    await create_via_ui(h, (30, "reg0030"), "registrar", name="Pending Team")
    async with pool.acquire() as c:
        tid = await c.fetchval("SELECT id FROM teams")
    h.clear()
    await h.press(ADMIN_ID, "adm", f"adm:tc:{tid}")
    t = h.last()
    assert "Pending Team" in t and "Капитан: ожидается" in t and "Регистратор:" in t and "Состав: 0/5" in t and "None" not in t
