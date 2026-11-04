"""Deep-link join and team management through the real dispatcher (fake Telegram transport)."""
import asyncio
import re

import pytest
import pytest_asyncio

from app.services.registration import RegistrationService, TgUser
from tests.harness import Harness
from tests.helpers import make_team, member_of, team_token

pytestmark = pytest.mark.db

CAP = (10, "cap0001")          # captain of make_team(pool, 1)
P2 = (2, None)                 # a player without username
P3 = (3, "player03")


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


async def members(pool):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT count(*) FROM team_members")


async def fresh_team(h, pool):
    """Captain creates a team through the UI; returns the invite token found in the bot's own message."""
    await h.press(*CAP, "reg:start")
    await h.press(*CAP, "reg:team")
    await h.say(*CAP, "Alpha Team")
    await h.press(*CAP, "reg:trole:captain")
    await h.press(*CAP, "reg:confirm")
    return re.search(r"start=team_([A-Za-z0-9_-]{32,48})", h.last()).group(1)


def sent_to(h, chat_id):
    return [m.text for m in h.tg.requests if getattr(m, "chat_id", None) == chat_id and getattr(m, "text", None)]


# ------------------------------------------------------------------ invitation & join
async def test_deep_link_shows_invitation_without_joining(h, pool):
    token = await fresh_team(h, pool)
    h.clear()
    await h.say(*P2, f"/start team_{token}")
    text = h.last()
    assert "Приглашение в команду" in text and "Alpha Team" in text and "Капитан" in text and "Участников: 1/5" in text
    assert {"✅ Вступить в команду", "❌ Отмена"} <= set(h.buttons())
    assert await members(pool) == 1  # opening the link never joins


async def test_join_flow_notifies_captain_once(h, pool):
    token = await fresh_team(h, pool)
    await h.say(*P2, f"/start team_{token}")
    h.clear()
    await asyncio.gather(*[h.press(*P2, f"join:ok:{token}") for _ in range(8)])  # double/multi tap
    assert await members(pool) == 2
    assert any("Вы вступили в команду" in t and "2/5" in t and "Alpha Team" in t for t in h.texts())
    assert "👥 Моя команда" in h.buttons()
    told = [t for t in sent_to(h, CAP[0]) if "Новый участник команды" in t]
    assert len(told) == 1 and "Состав: 2/5" in told[0] and "None" not in told[0]


async def test_reopening_link_by_member_is_friendly(h, pool):
    token = await fresh_team(h, pool)
    await h.press(*P2, f"join:ok:{token}")
    h.clear()
    await h.say(*P2, f"/start team_{token}")
    assert "Вы уже состоите в этой команде" in h.last() and "👥 Моя команда" in h.buttons()
    await h.press(*P2, f"join:ok:{token}")
    assert "Вы уже состоите в этой команде" in h.last()
    assert await members(pool) == 2
    h.clear()
    await h.press(*CAP, f"join:ok:{token}")  # captain clicking their own link
    assert "уже состоите" in h.last() and await members(pool) == 2


async def test_full_team_link(h, pool):
    tid = await make_team(pool, 1)
    token = await team_token(pool, tid)
    h.clear()
    await h.say(*P2, f"/start team_{token}")
    assert "В команде больше нет свободных мест" in h.last() and "✅ Вступить в команду" not in h.buttons()
    await h.press(*P2, f"join:ok:{token}")  # forged press on a stale invitation
    assert "В команде больше нет свободных мест" in h.last() and await members(pool) == 5


async def test_player_in_another_team_gets_warning(h, pool):
    await make_team(pool, 2, size=2)
    tid = await make_team(pool, 1, size=3)
    token = await team_token(pool, tid)
    h.clear()
    other = (member_of(2, 1).id, "mem0021")
    await h.say(*other, f"/start team_{token}")
    assert "Вы уже состоите в другой команде" in h.last() and "Сначала необходимо покинуть" in h.last()
    await h.press(*other, f"join:ok:{token}")
    assert "Вы уже состоите в другой команде" in h.last()
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE user_id = $1", other[0]) == 1


@pytest.mark.parametrize("payload", ["team_garbage", "team_", "ref_12345", "team_" + "a" * 100, "TEAM_" + "a" * 32,
                                     "team_" + "a" * 31 + "'", "1; DROP TABLE teams", "team_../../etc/passwd" + "a" * 20])
async def test_malformed_payload_behaves_like_plain_start(h, pool, payload):
    await h.say(*P2, f"/start {payload}")
    assert "Турнир Mobile Legends" in h.last() and "Приглашение" not in h.last()
    assert await members(pool) == 0


async def test_nonexistent_and_deleted_team_tokens(h, pool):
    await h.say(*P2, "/start team_" + "A" * 32)
    assert "недействительно" in h.last()
    token = await fresh_team(h, pool)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM teams")
    h.clear()
    await h.say(*P2, f"/start team_{token}")
    assert "недействительно" in h.last()
    await h.press(*P2, f"join:ok:{token}")
    assert "недействительно" in h.last() and await members(pool) == 0


@pytest.mark.parametrize("data", ["join:ok:", "join:ok:short", "join:ok:" + "a" * 49, "join:ok:" + "a" * 31 + "!", "join:", "join:ok",
                                  "join:ok:" + "%" * 32, "join:no:" + "a" * 32])
async def test_forged_join_callbacks(h, pool, data):
    await fresh_team(h, pool)
    before = await members(pool)
    await h.press(*P2, data)
    assert await members(pool) == before
    assert h.answers()  # answered (spinner stopped)


async def test_subscription_gate_on_join(h, pool):
    token = await fresh_team(h, pool)
    h.tg.chat_state["@MLBangBangTournamentChat"] = "left"
    h.clear()
    await h.press(*P2, f"join:ok:{token}")
    assert await members(pool) == 1
    assert {"📢 Подписаться на канал", "💬 Вступить в чат", "🔄 Проверить подписку"} <= set(h.buttons())
    h.tg.chat_state["@MLBangBangTournamentChat"] = "member"
    await h.press(*P2, f"join:ok:{token}")  # "Проверить подписку" re-runs the join
    assert await members(pool) == 2


async def test_registration_closed_blocks_join(h, pool):
    token = await fresh_team(h, pool)
    await h.svc.reg.set_open(False)
    h.clear()
    await h.say(*P2, f"/start team_{token}")
    assert "закрыта" in h.last()
    await h.press(*P2, f"join:ok:{token}")
    assert await members(pool) == 1


async def test_solo_player_cannot_join(h, pool):
    token = await fresh_team(h, pool)
    await RegistrationService(pool).register_solo(TgUser(P3[0], P3[1], "P3"), "EXP")
    h.clear()
    await h.press(*P3, f"join:ok:{token}")
    assert "соло" in h.last() and await members(pool) == 1


# ------------------------------------------------------------------ captain UI
async def test_captain_team_card_and_invite_screen(h, pool):
    token = await fresh_team(h, pool)
    await h.press(*P2, f"join:ok:{token}")
    h.clear()
    await h.press(*CAP, "team")
    t = h.last()
    assert "Alpha Team" in t and "Состав: 2/5" in t and "1. 👑" in t and "2. U2" in t and "None" not in t
    assert {"🔗 Пригласить игрока", "🔄 Управление составом"} <= set(h.buttons())
    h.clear()
    await h.press(*CAP, "team:inv")
    assert "Свободных мест: 3" in h.last() and f"start=team_{token}" in h.last()


async def test_invite_screen_when_full_and_link_still_shown(h, pool):
    await make_team(pool, 1)
    h.clear()
    await h.press(*CAP, "team:inv")
    assert "Свободных мест: 0" in h.last() and "start=team_" in h.last() and "заполнена" in h.last()


async def test_replace_flow_end_to_end(h, pool):
    tid = await make_team(pool, 1)
    token = await team_token(pool, tid)
    victim = member_of(1, 2)
    h.clear()
    await h.press(*CAP, "team:mng")
    assert len([b for b in h.buttons() if "Member" in b]) == 4 and not any("Captain" in b for b in h.buttons())  # captain not listed
    await h.press(*CAP, f"team:m:{victim.id}")
    assert {"🔄 Заменить", "🗑 Удалить из команды", "⬅️ Назад"} <= set(h.buttons())
    await h.press(*CAP, f"team:rp:{victim.id}")
    assert "Заменить участника?" in h.last() and "будет удалён из команды" in h.last() and "освободится одно место" in h.last()
    assert {"✅ Подтвердить", "❌ Отмена"} <= set(h.buttons())
    assert await members(pool) == 5  # not yet
    h.clear()
    await h.press(*CAP, f"team:rpok:{victim.id}")
    assert await members(pool) == 4
    shown = sent_to(h, CAP[0])[-1]  # what the captain sees (last() would be the notification to the removed player)
    assert "удалён из команды" in shown and "Свободных мест: 1" in shown and f"start=team_{token}" in shown
    assert any("удалил вас из команды" in t for t in sent_to(h, victim.id))  # removed player is told
    await h.press(*P2, f"join:ok:{token}")  # newcomer without username takes the seat
    assert await members(pool) == 5
    h.clear()
    await h.press(victim.id, "mem0012", "team")  # removed player: stale card
    assert "не состоите в команде" in h.last()


async def test_remove_cancel_and_double_confirm(h, pool):
    await make_team(pool, 1)
    victim = member_of(1, 1)
    await h.press(*CAP, f"team:rm:{victim.id}")
    assert "Удалить из команды?" in h.last()
    await h.press(*CAP, f"team:m:{victim.id}")  # the "Отмена" button goes back to the card
    assert await members(pool) == 5
    await asyncio.gather(*[h.press(*CAP, f"team:rmok:{victim.id}") for _ in range(6)])
    assert await members(pool) == 4
    h.clear()
    await h.press(*CAP, f"team:rmok:{victim.id}")
    assert "не найдена" in h.last() or "не найден" in h.last() or "уже" in h.last()
    assert await members(pool) == 4


async def test_captain_cannot_remove_self_via_forged_callback(h, pool):
    await make_team(pool, 1)
    for data in (f"team:rmok:{CAP[0]}", f"team:rpok:{CAP[0]}", f"team:rm:{CAP[0]}", f"team:m:{CAP[0]}"):
        await h.press(*CAP, data)
    assert await members(pool) == 5


async def test_regular_member_cannot_manage_roster(h, pool):
    await make_team(pool, 1)
    me = (member_of(1, 1).id, "mem0011")
    other = member_of(1, 2).id
    h.clear()
    for data in ("team:mng", "team:inv", f"team:m:{other}", f"team:rp:{other}", f"team:rm:{other}", f"team:rmok:{other}",
                 f"team:rpok:{other}", f"team:rmok:{CAP[0]}"):
        await h.press(*me, data)
    assert await members(pool) == 5
    assert "только капитану" in h.last()


async def test_captain_cannot_touch_another_teams_members(h, pool):
    await make_team(pool, 1)
    await make_team(pool, 2)
    foreign = member_of(2, 1).id
    for data in (f"team:m:{foreign}", f"team:rm:{foreign}", f"team:rmok:{foreign}", f"team:rpok:{foreign}"):
        await h.press(*CAP, data)
    assert await members(pool) == 10


@pytest.mark.parametrize("data", ["team:m:abc", "team:m:0", "team:rmok:" + "9" * 40, "team:x", "team:rmok:-5", "team:m:", "team:leaveok:1"])
async def test_forged_team_callbacks_are_harmless(h, pool, data):
    await make_team(pool, 1)
    await h.press(*CAP, data)
    await h.press(9999, "stranger9", data)
    assert await members(pool) == 5 and h.answers()


# ------------------------------------------------------------------ leaving
async def test_member_leaves_with_confirmation_and_captain_cannot(h, pool):
    await make_team(pool, 1)
    me = (member_of(1, 3).id, "mem0013")
    h.clear()
    await h.press(*me, "team")
    assert "🚪 Покинуть команду" in h.buttons() and "🔄 Управление составом" not in h.buttons()
    await h.press(*me, "team:leave")
    assert "Покинуть команду?" in h.last() and await members(pool) == 5
    await h.press(*me, "team:leaveok")
    assert await members(pool) == 4 and "Вы покинули команду" in sent_to(h, me[0])[-1]
    assert any("покинул" in t and "4/5" in t for t in sent_to(h, CAP[0]))
    await h.press(*me, "team:leaveok")  # double click
    assert await members(pool) == 4
    h.clear()
    await h.press(*CAP, "team")
    assert "🚪 Покинуть команду" not in h.buttons()
    await h.press(*CAP, "team:leave")
    assert "Капитан не может покинуть команду" in h.last()
    await h.press(*CAP, "team:leaveok")
    assert "Капитан не может" in h.last() or "администратору" in h.last()
    assert await members(pool) == 4


async def test_menu_shows_team_button_for_members(h, pool):
    await make_team(pool, 1, size=2)
    await h.say(member_of(1, 1).id, None, "/start")
    assert "👥 Моя команда" in h.buttons() and "🏆 Регистрация" not in h.buttons()


async def test_admin_views_team_without_usernames(h, pool):
    from app.services.registration import TgUser
    from app.services.team import TeamService

    svc = TeamService(pool)
    v = await svc.create_team(TgUser(1, None, "Безымянный Капитан"), "NoNames")
    await svc.join_team(TgUser(2, None, ""), v.invite_token)
    from tests.harness import ADMIN_ID

    h.clear()
    await h.press(ADMIN_ID, "adm", f"adm:tc:{v.team_id}")
    t = h.last()
    assert "Безымянный Капитан" in t and "Игрок 2" in t and "None" not in t and "@None" not in t
    await h.press(ADMIN_ID, "adm", "adm:t:0")
    assert "NoNames" in h.buttons()


async def test_broadcast_all_reaches_every_team_member(h, pool):
    from app.repositories import broadcasts as bc_repo

    await make_team(pool, 1)
    async with pool.acquire() as c:
        got = await bc_repo.recipients(c, "all")
    assert sorted(got) == sorted([10, 11, 12, 13, 14])
    async with pool.acquire() as c:
        assert await bc_repo.recipients(c, "captains") == [10]
