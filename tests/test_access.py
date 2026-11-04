"""Closed-testing access mode: config parsing, can_access_bot vs is_bot_admin, and the global gate."""
import pytest
import pytest_asyncio
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Message, Update
from datetime import datetime

from app.config import Config, ConfigError, load_config, parse_allowed_ids
from app.handlers.registration import RegTeam
from app.middlewares.access import DENIED_ALERT, DENIED_TEXT
from app.services.registration import TgUser
from tests.harness import ADMIN_ID, TOKEN, Harness
from tests.helpers import make_team

TESTER = 6323637322
STRANGER = 555000111
DSN = "postgresql://mlbb_bot:s3cret@127.0.0.1:5432/mlbb_tournament"
GOOD = {"BOT_TOKEN": TOKEN, "ADMIN_TELEGRAM_ID": str(ADMIN_ID), "DATABASE_URL": DSN}


def load(extra, tmp_path):
    return load_config(env_file=tmp_path / "none.env", environ={**GOOD, **extra})


# ------------------------------------------------------------------ config
def test_default_mode_is_private_fail_closed(tmp_path):
    cfg = load({}, tmp_path)
    assert cfg.access_mode == "private" and cfg.allowed_user_ids == frozenset()


def test_modes_parse(tmp_path):
    assert load({"ACCESS_MODE": "private"}, tmp_path).access_mode == "private"
    assert load({"ACCESS_MODE": "public"}, tmp_path).access_mode == "public"
    assert load({"ACCESS_MODE": "  PUBLIC "}, tmp_path).access_mode == "public"


@pytest.mark.parametrize("bad", ["open", "Private1", "true", "0", "everyone", "pub lic"])
def test_invalid_access_mode_fails_closed_at_startup(bad, tmp_path):
    with pytest.raises(ConfigError, match="ACCESS_MODE"):
        load({"ACCESS_MODE": bad}, tmp_path)


def test_blank_access_mode_in_env_file_is_an_error_not_public(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"BOT_TOKEN={TOKEN}\nADMIN_TELEGRAM_ID=1\nDATABASE_URL={DSN}\nACCESS_MODE=\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ACCESS_MODE"):
        load_config(env_file=f, environ={})


def test_allowlist_multiple_ids_whitespace_and_duplicates(tmp_path):
    cfg = load({"ALLOWED_USER_IDS": " 6323637322 , 123456789,987654321 ,6323637322,123456789,"}, tmp_path)
    assert cfg.allowed_user_ids == frozenset({6323637322, 123456789, 987654321})


def test_allowlist_single_and_empty(tmp_path):
    assert load({"ALLOWED_USER_IDS": "6323637322"}, tmp_path).allowed_user_ids == {TESTER}
    assert load({}, tmp_path).allowed_user_ids == frozenset()
    assert parse_allowed_ids("") == frozenset() and parse_allowed_ids(" , ,") == frozenset()


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "1.5", "12,x", "١٢٣", "1 2", "0x10", "@name", "9" * 30])
def test_malformed_allowlist_detected_at_startup(bad, tmp_path):
    with pytest.raises(ConfigError) as e:
        load({"ALLOWED_USER_IDS": bad}, tmp_path)
    assert "ALLOWED_USER_IDS" in str(e.value) and bad not in str(e.value)  # does not echo the value


def cfg_of(mode="private", allowed=(TESTER,), admin=ADMIN_ID):
    return Config(bot_token=TOKEN, admin_id=admin, database_url=DSN, channel_id="@yuliataetae",
                  channel_url="https://t.me/x", chat_id="@MLBangBangTournamentChat", chat_url="https://t.me/y",
                  access_mode=mode, allowed_user_ids=frozenset(allowed))


# ------------------------------------------------------------------ access vs admin separation
def test_admin_passes_and_is_admin_even_if_not_in_allowlist():
    c = cfg_of()
    assert c.can_access_bot(ADMIN_ID) is True and c.is_bot_admin(ADMIN_ID) is True


def test_allowed_user_has_access_but_is_not_admin():
    c = cfg_of()
    assert c.can_access_bot(TESTER) is True
    assert c.is_bot_admin(TESTER) is False


def test_unknown_user_blocked_in_private_mode():
    c = cfg_of()
    assert c.can_access_bot(STRANGER) is False and c.is_bot_admin(STRANGER) is False


def test_public_mode_opens_access_but_never_admin():
    c = cfg_of("public")
    assert c.can_access_bot(STRANGER) is True and c.is_bot_admin(STRANGER) is False


@pytest.mark.parametrize("mode", ["", "PRIVATE", "closed", None, "public "])
def test_unknown_mode_object_fails_closed(mode):
    c = cfg_of(mode)
    assert c.can_access_bot(STRANGER) is False and c.can_access_bot(TESTER) is True  # allowlist only


def test_allowlisting_someone_in_admin_slot_does_not_leak_admin():
    assert cfg_of(allowed=(TESTER, 1, 2)).is_bot_admin(1) is False


# ------------------------------------------------------------------ gate (no database at all)
class ExplodingPool:
    """Any use means a denied user reached business logic / SQL."""
    touched = False

    def acquire(self, *a, **k):
        ExplodingPool.touched = True
        raise AssertionError("database touched")

    def __getattr__(self, name):
        ExplodingPool.touched = True
        raise AssertionError("database touched")


@pytest_asyncio.fixture
async def g():
    ExplodingPool.touched = False
    h = Harness(ExplodingPool(), access_mode="private", allowed=[TESTER])
    yield h
    assert not h.errors, [r.getMessage() for r in h.errors]
    assert ExplodingPool.touched is False
    h.close()


def assert_denied_message(h):
    assert h.texts() == [DENIED_TEXT]
    assert "Доступ к боту ограничен" in DENIED_TEXT and "закрытого тестирования" in DENIED_TEXT
    assert h.buttons() == []  # no menu leaks through


async def test_blocked_start(g):
    await g.say(STRANGER, "stranger1", "/start")
    assert_denied_message(g)


@pytest.mark.parametrize("text", ["/start", "/start ref_123", "/start admin", "/admin", "/cancel", "/whatever", "/help",
                                  "hello", "@captainone", "Team Name", "/start@SomeBot payload"])
async def test_blocked_commands_deeplinks_and_plain_text(g, text):
    await g.say(STRANGER, "stranger1", text)
    assert_denied_message(g)


ALL_CALLBACKS = ["menu", "me", "bracket", "noop", "reg:start", "reg:team", "reg:solo", "reg:confirm", "reg:cancel",
                 "reg:back", "reg:role:EXP", "sub:check", "adm", "adm:t:0", "adm:tc:1", "adm:tdok:1", "adm:s:0",
                 "adm:reg:set:0", "adm:m", "mt:new", "mt:ok", "mt:wok:1:1", "mt:dok:1", "adm:bc", "bc:aud:all",
                 "bc:send:1", "garbage", "adm:tc:" + "9" * 40, "x" * 64]


@pytest.mark.parametrize("data", ALL_CALLBACKS)
async def test_blocked_callback_is_answered_and_never_reaches_handlers(g, data):
    await g.press(STRANGER, "stranger1", data)
    answers = g.answers()
    assert len(answers) == 1 and answers[0].text == DENIED_ALERT and answers[0].show_alert  # spinner stopped
    assert len(g.tg.requests) == 1  # nothing else was sent or edited


async def test_old_admin_button_from_old_message_is_blocked_for_stranger(g):
    # a callback from a "previous session" message: identical payload, still refused by the gate
    await g.press(STRANGER, "stranger1", "adm:tdok:1")
    await g.press(STRANGER, "stranger1", "bc:send:1")
    assert all(a.text == DENIED_ALERT for a in g.answers()) and len(g.tg.requests) == 2


async def test_blocked_user_with_leftover_fsm_state_cannot_continue(g):
    """State that outlived an allowlist change must not let the user finish a flow."""
    key = StorageKey(bot_id=g.bot.id, chat_id=STRANGER, user_id=STRANGER)
    await g.dp.storage.set_state(key, RegTeam.confirm)
    await g.dp.storage.set_data(key, {"name": "Sneaky", "members": ["a1234", "b1234", "c1234", "d1234"]})
    await g.press(STRANGER, "stranger1", "reg:confirm")
    await g.press(STRANGER, "stranger1", "sub:check")
    await g.say(STRANGER, "stranger1", "@a1234")
    assert len(g.answers()) == 2 and DENIED_TEXT in g.texts()
    assert len(g.tg.requests) == 3


async def test_no_configuration_leak_in_denials(g):
    await g.say(STRANGER, "stranger1", "/start")
    await g.press(STRANGER, "stranger1", "adm")
    blob = " ".join(g.texts()) + " ".join(a.text or "" for a in g.answers())
    for secret in (str(ADMIN_ID), str(TESTER), "ALLOWED", "ADMIN", "allowlist", "ACCESS_MODE"):
        assert secret not in blob


async def test_updates_without_a_user_are_dropped_in_private_mode(g):
    post = Message(message_id=1, date=datetime.datetime('now'), chat=Chat(id=-100123, type="channel"), text="hi")
    await g.dp.feed_update(g.bot, Update(update_id=1, channel_post=post))
    assert g.tg.requests == []


async def test_denied_user_in_group_chat_gets_no_reply(g):
    await g.say(STRANGER, "stranger1", "/start", chat_type="supergroup")
    assert g.tg.requests == []


async def test_two_users_are_evaluated_independently(g):
    await g.say(STRANGER, "stranger1", "/start")
    await g.say(777, "another01", "/start")
    assert g.texts() == [DENIED_TEXT, DENIED_TEXT]


# ------------------------------------------------------------------ allowed users (need PostgreSQL)
@pytest_asyncio.fixture
async def hp(pool):
    h = Harness(pool, access_mode="private", allowed=[TESTER])
    yield h
    assert not h.errors, [r.getMessage() for r in h.errors]
    h.close()


async def counts(pool):
    async with pool.acquire() as c:
        return [await c.fetchval(f"SELECT count(*) FROM {t}") for t in
                ("users", "teams", "team_members", "solo_players", "matches", "broadcasts")]


@pytest.mark.db
async def test_tester_gets_menu_without_admin_rights(hp):
    await hp.say(TESTER, "tester0001", "/start")
    b = hp.buttons()
    assert "🏆 Регистрация" in b and "🏆 Турнирная сетка" in b and "🛠 Админ-панель" not in b
    assert DENIED_TEXT not in hp.texts()


@pytest.mark.db
async def test_admin_passes_private_mode_and_sees_admin_panel(hp):
    await hp.say(ADMIN_ID, "theadmin", "/start")
    assert "🛠 Админ-панель" in hp.buttons()
    hp.clear()
    await hp.say(ADMIN_ID, "theadmin", "/admin")
    assert "Админ-панель" in hp.last()


@pytest.mark.db
@pytest.mark.parametrize("data", ["adm", "adm:t:0", "adm:tdok:1", "adm:reg:set:0", "mt:new", "adm:bc", "bc:send:1"])
async def test_tester_cannot_use_admin_even_if_telegram_chat_admin(hp, pool, data):
    """Telegram-side chat admin status is never consulted: no getChatMember call, no admin screen."""
    await make_team(pool, 1)
    hp.tg.chat_state["@MLBangBangTournamentChat"] = "member"  # would even say 'member/administrator'
    hp.clear()
    before = (await counts(pool))[1:]  # `users` legitimately gains the tester's tracking row
    await hp.press(TESTER, "tester0001", data)
    await hp.say(TESTER, "tester0001", "/admin")
    assert (await counts(pool))[1:] == before
    assert not any(type(m).__name__ == "GetChatMember" for m in hp.tg.requests)
    shown = " ".join(hp.texts())
    assert "Админ-панель" not in shown and "Всего" not in shown
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT value FROM settings WHERE key='registration_open'") == "true"


@pytest.mark.db
async def test_tester_can_register_normally(hp, pool):
    await hp.press(TESTER, "tester0001", "reg:solo")
    await hp.press(TESTER, "tester0001", "reg:role:MID")
    await hp.press(TESTER, "tester0001", "reg:confirm")
    assert (await counts(pool))[3] == 1


@pytest.mark.db
async def test_blocked_users_never_write_to_the_database(hp, pool):
    before = await counts(pool)
    for cb in ALL_CALLBACKS:
        await hp.press(STRANGER, "stranger1", cb)
    for txt in ["/start", "/start payload", "/admin", "Team", "@aaaaa1", "hello"]:
        await hp.say(STRANGER, "stranger1", txt)
    assert await counts(pool) == before == [0] * 6  # not even a `users` row for the tracker


@pytest.mark.db
async def test_blocked_user_cannot_finish_a_flow_started_before_removal(pool):
    """Allowed at first, then the allowlist no longer contains them: the very next update is refused."""
    first = Harness(pool, access_mode="private", allowed=[TESTER, STRANGER])
    await first.press(STRANGER, "stranger1", "reg:solo")
    await first.press(STRANGER, "stranger1", "reg:role:EXP")
    key = StorageKey(bot_id=first.bot.id, chat_id=STRANGER, user_id=STRANGER)
    state, data = await first.dp.storage.get_state(key), await first.dp.storage.get_data(key)
    first.close()
    second = Harness(pool, access_mode="private", allowed=[TESTER])  # STRANGER removed from the list
    await second.dp.storage.set_state(key, state)
    await second.dp.storage.set_data(key, data)
    await second.press(STRANGER, "stranger1", "reg:confirm")
    assert (await counts(pool))[3] == 0
    assert second.answers()[-1].text == DENIED_ALERT
    second.close()


@pytest.mark.db
async def test_public_mode_lets_anyone_in_but_not_admin(pool):
    h = Harness(pool, access_mode="public")
    try:
        await h.say(STRANGER, "stranger1", "/start")
        assert "🏆 Регистрация" in h.buttons() and "🛠 Админ-панель" not in h.buttons()
        h.clear()
        await h.press(STRANGER, "stranger1", "adm:t:0")
        assert "Всего" not in " ".join(h.texts())
    finally:
        h.close()


@pytest.mark.db
async def test_bot_ignores_group_chat_messages_even_from_allowed_users(hp):
    await hp.say(TESTER, "tester0001", "/start", chat_type="supergroup")
    await hp.say(ADMIN_ID, "theadmin", "hello", chat_type="group")
    assert hp.tg.requests == []


# ------------------------------------------------------------------ invite links never bypass private mode
VALID_LOOKING_TOKEN = "A" * 32


@pytest.mark.parametrize("payload", [f"team_{VALID_LOOKING_TOKEN}", "team_garbage", "ref_1"])
async def test_deep_link_from_stranger_is_denied_without_touching_db(g, payload):
    await g.say(STRANGER, "stranger1", f"/start {payload}")
    assert_denied_message(g)  # same generic message: does not even reveal whether the link is valid


@pytest.mark.parametrize("data", [f"join:ok:{VALID_LOOKING_TOKEN}", "team", "team:inv", "team:mng", "team:leaveok", "team:rmok:1", "team:m:5"])
async def test_team_callbacks_from_stranger_are_denied(g, data):
    await g.press(STRANGER, "stranger1", data)
    assert [a.text for a in g.answers()] == [DENIED_ALERT] and len(g.tg.requests) == 1


@pytest.mark.db
async def test_stranger_with_a_real_invite_cannot_join_in_private_mode(hp, pool):
    tid = await make_team(pool, 1, size=2)
    async with pool.acquire() as c:
        token = await c.fetchval("SELECT invite_token FROM teams WHERE id = $1", tid)
    hp.clear()
    await hp.say(STRANGER, "stranger1", f"/start team_{token}")
    await hp.press(STRANGER, "stranger1", f"join:ok:{token}")
    assert hp.texts() == [DENIED_TEXT] and hp.answers()[-1].text == DENIED_ALERT
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 2
        assert await c.fetchval("SELECT count(*) FROM users WHERE telegram_id = $1", STRANGER) == 0


@pytest.mark.db
async def test_allowed_tester_can_join_by_invite_in_private_mode(hp, pool):
    tid = await make_team(pool, 1, size=2)
    async with pool.acquire() as c:
        token = await c.fetchval("SELECT invite_token FROM teams WHERE id = $1", tid)
    hp.clear()
    await hp.say(TESTER, "tester0001", f"/start team_{token}")
    assert "Приглашение в команду" in hp.last()
    await hp.press(TESTER, "tester0001", f"join:ok:{token}")
    assert "Вы вступили в команду" in " ".join(hp.texts())
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE user_id = $1", TESTER) == 1


@pytest.mark.db
async def test_tester_who_joined_is_not_admin_of_the_team_or_bot(hp, pool):
    tid = await make_team(pool, 1, size=2)
    async with pool.acquire() as c:
        token = await c.fetchval("SELECT invite_token FROM teams WHERE id = $1", tid)
    await hp.press(TESTER, "tester0001", f"join:ok:{token}")
    hp.clear()
    victim = 11
    await hp.press(TESTER, "tester0001", f"team:rmok:{victim}")
    await hp.press(TESTER, "tester0001", "adm:t:0")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 3
    assert "только капитану" in " ".join(hp.texts()) and "Всего" not in " ".join(hp.texts())


# ------------------------------------------------------------------ captain links never bypass private mode
@pytest.mark.parametrize("payload", [f"captain_{VALID_LOOKING_TOKEN}", "captain_garbage", f"team_{VALID_LOOKING_TOKEN}"])
async def test_captain_deep_link_from_stranger_is_denied_without_touching_db(g, payload):
    await g.say(STRANGER, "stranger1", f"/start {payload}")
    assert_denied_message(g)


@pytest.mark.parametrize("data", [f"cap:ok:{VALID_LOOKING_TOKEN}", "team:cap", "team:capreset", "team:cancelok", "team:tr",
                                  "team:trc:5", "team:trok:5", "reg:trole:captain", "reg:trole:member", "reg:trole:registrar"])
async def test_role_callbacks_from_stranger_are_denied(g, data):
    await g.press(STRANGER, "stranger1", data)
    assert [a.text for a in g.answers()] == [DENIED_ALERT] and len(g.tg.requests) == 1


@pytest.mark.db
async def test_stranger_with_real_captain_and_player_links_is_denied_in_private_mode(hp, pool):
    from app.services.team import TeamService

    svc = TeamService(pool)
    v = await svc.create_team(TgUser(ADMIN_ID, None, "Admin"), "Private Team", "registrar", is_admin=True)
    hp.clear()
    for payload in (f"captain_{v.captain_invite_token}", f"team_{v.invite_token}"):
        await hp.say(STRANGER, "stranger1", f"/start {payload}")
    await hp.press(STRANGER, "stranger1", f"cap:ok:{v.captain_invite_token}")
    await hp.press(STRANGER, "stranger1", f"join:ok:{v.invite_token}")
    assert hp.texts() == [DENIED_TEXT, DENIED_TEXT] and all(a.text == DENIED_ALERT for a in hp.answers())
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 0
        assert await c.fetchval("SELECT captain_id FROM teams") is None
        assert await c.fetchval("SELECT count(*) FROM users WHERE telegram_id = $1", STRANGER) == 0


@pytest.mark.db
async def test_allowed_tester_can_accept_captain_but_stays_a_non_admin(hp, pool):
    from app.services.team import TeamService

    v = await TeamService(pool).create_team(TgUser(ADMIN_ID, None, "Admin"), "Private Team", "registrar", is_admin=True)
    hp.clear()
    await hp.say(TESTER, "tester0001", f"/start captain_{v.captain_invite_token}")
    assert "Приглашение капитана" in hp.last()
    await hp.press(TESTER, "tester0001", f"cap:ok:{v.captain_invite_token}")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == TESTER
    hp.clear()
    await hp.press(TESTER, "tester0001", "adm:t:0")         # captain of a team is still no bot admin
    assert "Всего" not in " ".join(hp.texts())


@pytest.mark.db
async def test_solo_tester_like_the_real_record_cannot_take_captaincy(hp, pool):
    """The real 6323637322 is registered solo (GOLD): the guard must refuse without touching that record."""
    from app.services.registration import RegistrationService
    from app.services.team import TeamService

    await RegistrationService(pool).register_solo(TgUser(TESTER, "tester0001", "Tester"), "GOLD")
    v = await TeamService(pool).create_team(TgUser(ADMIN_ID, None, "Admin"), "Private Team", "registrar", is_admin=True)
    hp.clear()
    await hp.press(TESTER, "tester0001", f"cap:ok:{v.captain_invite_token}")
    assert "соло" in hp.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT role FROM solo_players WHERE user_id = $1", TESTER) == "GOLD"
        assert await c.fetchval("SELECT captain_id FROM teams") is None


# ------------------------------------------------------------------ public release: whitelist must not restrict, admin stays closed
@pytest.fixture
async def hpub(pool):
    """Public mode while ALLOWED_USER_IDS is still filled in .env (as in the real deployment)."""
    h = Harness(pool, access_mode="public", allowed=[TESTER])
    yield h
    assert not h.errors, [r.getMessage() for r in h.errors]
    h.close()


@pytest.mark.db
async def test_public_mode_ordinary_user_outside_whitelist_gets_normal_flow(hpub, pool):
    await hpub.say(STRANGER, "stranger1", "/start")
    assert DENIED_TEXT not in hpub.texts() and "🏆 Регистрация" in hpub.buttons() and "🛠 Админ-панель" not in hpub.buttons()
    async with pool.acquire() as c:  # /start alone registers nothing
        assert await c.fetchval("SELECT count(*) FROM teams") == 0
        assert await c.fetchval("SELECT count(*) FROM solo_players") == 0
        assert await c.fetchval("SELECT count(*) FROM team_members") == 0


@pytest.mark.db
async def test_public_mode_deep_links_work_for_ordinary_user_but_business_rules_hold(hpub, pool):
    from app.services.team import TeamService

    svc = TeamService(pool)
    v = await svc.create_team(TgUser(ADMIN_ID, None, "Admin"), "Public Team", "registrar", is_admin=True)
    await hpub.say(STRANGER, "stranger1", f"/start captain_{v.captain_invite_token}")
    assert "Приглашение капитана" in hpub.last() and DENIED_TEXT not in hpub.texts()
    await hpub.say(STRANGER, "stranger1", f"/start team_{v.invite_token}")
    assert "Капитан этой команды пока не назначен" in hpub.last()          # captain rule still applies
    hpub.tg.chat_state["@yuliataetae"] = "left"
    hpub.clear()
    await hpub.press(STRANGER, "stranger1", f"cap:ok:{v.captain_invite_token}")
    assert "Подписаться на канал" in " ".join(hpub.buttons())               # subscription gate still applies
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 0
    hpub.tg.chat_state["@yuliataetae"] = "member"
    await hpub.press(STRANGER, "stranger1", f"cap:ok:{v.captain_invite_token}")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT captain_id FROM teams") == STRANGER


ADMIN_ONLY = ["adm", "adm:t:0", "adm:tc:1", "adm:tdok:1", "adm:s:0", "adm:sdok:5", "adm:reg", "adm:reg:set:0", "adm:m", "mt:new",
              "mt:ok", "mt:wok:1:1", "mt:dok:1", "adm:bc", "bc:aud:all", "bc:send:1"]


@pytest.mark.db
@pytest.mark.parametrize("data", ADMIN_ONLY)
async def test_public_mode_ordinary_and_whitelisted_users_cannot_use_admin(hpub, pool, data):
    from app.services.team import TeamService

    await TeamService(pool).create_team(TgUser(ADMIN_ID, None, "Admin"), "Guarded", "registrar", is_admin=True)
    async with pool.acquire() as c:
        before = [await c.fetchval(f"SELECT count(*) FROM {t}") for t in ("teams", "team_members", "solo_players", "matches", "broadcasts")]
        reg_before = await c.fetchval("SELECT value FROM settings WHERE key='registration_open'")
    hpub.clear()
    for uid, name in ((STRANGER, "stranger1"), (TESTER, "tester0001")):      # an ordinary user AND the old whitelisted tester
        await hpub.press(uid, name, data)
        await hpub.say(uid, name, "/admin")
    shown = " ".join(hpub.texts())
    assert "Админ-панель" not in shown and "Всего" not in shown
    async with pool.acquire() as c:
        after = [await c.fetchval(f"SELECT count(*) FROM {t}") for t in ("teams", "team_members", "solo_players", "matches", "broadcasts")]
        assert after == before and await c.fetchval("SELECT value FROM settings WHERE key='registration_open'") == reg_before


@pytest.mark.db
async def test_public_mode_admin_still_administers(hpub, pool):
    await hpub.press(ADMIN_ID, "theadmin", "adm")
    assert "Админ-панель" in hpub.last()
