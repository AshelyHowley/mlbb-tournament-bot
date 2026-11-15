"""📋 История регистраций: every event is logged with a full snapshot, shown as cards, exported to CSV."""
import csv
import io

import pytest
import pytest_asyncio
from aiogram.methods import SendDocument

from app.services.registration import RegistrationService, TgUser
from app.services.team import TeamService
from tests.harness import ADMIN_ID, Harness

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def h(pool):
    hh = Harness(pool)
    yield hh
    assert not hh.errors, [r.getMessage() for r in hh.errors]
    hh.close()


async def _log(pool):
    async with pool.acquire() as c:
        return await c.fetch("SELECT * FROM registration_log ORDER BY id")


async def test_solo_registration_logs_everything(h, pool):
    await RegistrationService(pool).register_solo(TgUser(501, "ivanpetrov", "Иван Петров"), "ROAM,MID", 1234)
    (r,) = await _log(pool)
    assert (r["action"], r["user_id"], r["username"], r["full_name"], r["roles"], r["stars"]) == \
        ("solo", 501, "ivanpetrov", "Иван Петров", "ROAM,MID", 1234)
    assert r["tournament_id"] is not None
    await h.press(ADMIN_ID, "adm", "log:reg")
    text = h.last()
    for part in ("Соло-регистрация", "Иван Петров", "@ivanpetrov", "501", "ROAM, MID", "⭐ 1234"):
        assert part in text, part


async def test_team_lifecycle_is_logged(pool):
    teams = TeamService(pool)
    v = await teams.create_team(TgUser(10, "captain10", "Кэп"), "Парламент", "captain", rank=900)
    await teams.join_team(TgUser(11, "player11", "Игрок"), v.invite_token, rank=700)
    await teams.join_team(TgUser(12, "player12", "Игрок2"), v.invite_token, rank=500)
    await teams.transfer_captaincy(10, 11)
    await teams.remove_member(11, 12)
    await teams.leave_team(10)
    rows = await _log(pool)
    assert [r["action"] for r in rows] == ["create_team", "join_team", "join_team", "captain_transfer", "removed", "leave"]
    create, join = rows[0], rows[1]
    assert (create["team_name"], create["member_role"], create["stars"]) == ("Парламент", "captain", 900)
    assert (join["user_id"], join["stars"], join["member_role"]) == (11, 700, "member")
    assert all(r["team_id"] == v.team_id for r in rows)


async def test_registrar_position_and_captain_claim(pool):
    teams = TeamService(pool)
    v = await teams.create_team(TgUser(20, "manager20", "Менеджер"), "GROM", "registrar", position="Менеджер")
    await teams.accept_captain(TgUser(21, "capnew21", "Новый кэп"), v.captain_invite_token)
    reg, cap = await _log(pool)
    assert (reg["member_role"], reg["position"], reg["stars"]) == ("registrar", "Менеджер", None)
    assert (cap["action"], cap["user_id"]) == ("captain", 21)


async def test_pages_and_old_format_rows(h, pool):
    async with pool.acquire() as c:  # rows written before migration 022 had only details
        await c.execute("INSERT INTO users (telegram_id, username, full_name) VALUES (777, 'oldman77', 'Старичок')")
        await c.execute("INSERT INTO registration_log (user_id, action, details) VALUES (777, 'solo', 'EXP,GOLD')")
        for i in range(9):
            await c.execute("INSERT INTO registration_log (user_id, action, details) VALUES (777, 'solo', 'MID')")
    await h.press(ADMIN_ID, "adm", "log:reg")
    assert "Всего записей: <b>10</b>" in h.last() and "стр. 1/2" in h.last()
    await h.press(ADMIN_ID, "adm", "log:reg:1")
    assert "стр. 2/2" in h.last() and "EXP, GOLD" in h.last() and "@oldman77" in h.last()


async def test_csv_export_has_all_columns(h, pool):
    await RegistrationService(pool).register_solo(TgUser(501, "ivanpetrov", "Иван; Петров"), "ROAM", 1000)
    await h.press(ADMIN_ID, "adm", "log:reg:csv")
    doc = next(m for m in h.tg.requests if isinstance(m, SendDocument))
    assert doc.document.filename.endswith(".csv")
    raw = doc.document.data
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM: Excel detects UTF-8
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:5] == ["Дата и время", "Событие", "Telegram ID", "Username", "Имя"]
    assert rows[1][1:5] == ["Соло-регистрация", "501", "@ivanpetrov", "Иван; Петров"]
    assert rows[1][7:9] == ["ROAM", "1000"]


async def test_names_are_escaped(h, pool):
    await RegistrationService(pool).register_solo(TgUser(502, "hacker502", "<b>x</b>"), "MID", 1)
    await h.press(ADMIN_ID, "adm", "log:reg")
    assert "<b>x</b>" not in h.last() and "&lt;b&gt;x" in h.last()
