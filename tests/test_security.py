import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.keyboards import admin as akb
from app.config import Config
from app.keyboards import common as ckb
from app.keyboards import team as tkb
from app.services.registration import TgUser
from app.services.team import TeamService
from tests.harness import ADMIN_ID, Harness
from tests.helpers import make_team

ROOT = Path(__file__).resolve().parent.parent
LONG = "Я" * 40


def _cb_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_callback_data_within_telegram_limit():
    big = 10**18
    rows = [{"id": big, "name": LONG, "user_id": big, "full_name": LONG, "username": "u" * 32, "role": "JUNGLE",
             "team_a": LONG, "team_b": LONG, "stage_title": "Полуфинал", "team_a_id": big, "team_b_id": big - 1,
             "candidate_id": big, "approved_at": datetime.datetime('now'), "completed_at": datetime.datetime('now')}]
    match = {**rows[0], "status": "scheduled"}
    members = [SimpleNamespace(user_id=big, full_name=LONG, username="u" * 32, is_captain=False)]
    games = [{"id": big, "game_number": 5, "winner_name": LONG}]
    game = games[0]
    markups = [
        akb.admin_menu(True), akb.admin_menu(False), akb.teams_list(rows, 999999, 999999), akb.team_card(big),
        akb.solo_list(rows, 5, 9), akb.solo_card(big), akb.registration(True), akb.matches_menu(),
        akb.pick_team(rows, "mt:b", 1, 9, 0), akb.pick_stage([{"code": "x" * 20, "title": "t"}]),
        akb.match_list(rows, "scheduled", 9, 9), akb.series_card(match), akb.game_winner_pick(match),
        akb.confirm_game(big, big, 5), akb.games_edit_list(big, games), akb.game_edit_pick(game, match),
        akb.confirm_game_edit(big, big, big), akb.bc_confirm(big), akb.bc_audience(),
        akb.confirm_delete(f"adm:tdok:{big}", f"adm:tc:{big}"), ckb.roles(),
        akb.sn_audience_series(big), akb.sn_audience_final(big), akb.sn_cancel(), akb.sn_confirm(big),
        akb.deputy_menu(9, 2), akb.deputy_list(rows), akb.deputy_card(big), akb.deputy_pending_list(rows),
        akb.deputy_review(big), akb.deputy_invitation("T" * 48), akb.finish_confirm(True), akb.finish_confirm(False),
        akb.finish_final(), akb.finish_done(), akb.history_list(rows, 999999, 999999), akb.history_card(big),
        akb.history_back(big), akb.history_teams_list(rows, big), akb.history_matches_list(rows, big),
        tkb.invitation("T" * 48), tkb.team_card("captain", pending=False, is_creator=True, can_transfer=True), tkb.team_card("member", pending=True, is_creator=True),
        tkb.captain_invitation("T" * 48), tkb.after_create_pending(), tkb.captain_link_screen(), tkb.transfer_list(members), tkb.after_create(), tkb.my_team_only(),
        tkb.member_card(big), tkb.confirm(f"team:rmok:{big}", f"team:m:{big}"), tkb.manage_list(members),
        ckb.subscribe(Config("1:x", 1, "d", "@a", "https://t.me/a", "@b", "https://t.me/b"), retry_cb=f"join:ok:{'T' * 48}", cancel_cb="menu"),
    ]
    for mk in markups:
        for data in _cb_data(mk):
            assert len(data.encode()) <= 64, data


def test_no_secrets_committed_to_source():
    token = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
    dsn = re.compile(r"postgres(?:ql)?://[^:\s]+:([^@\s]+)@")
    for path in ROOT.rglob("*"):
        if path.is_dir() or any(p in path.parts for p in (".git", "__pycache__", "logs", ".pytest_cache", "tests")):
            continue
        if path.name == ".env" or path.suffix not in {".py", ".md", ".txt", ".example", ".sql", ".ini", ""}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not token.search(text), f"bot-token-like string in {path}"
        for m in dsn.finditer(text):
            assert m.group(1) in {"PASSWORD", "***"} or "{" in m.group(1), f"password in {path}"


def test_env_example_has_all_keys_and_no_secrets():
    text = (ROOT / ".env.example").read_text()
    for key in ("BOT_TOKEN=", "ADMIN_TELEGRAM_ID=", "DATABASE_URL=", "CHANNEL_ID=@yuliataetae", "CHAT_ID=@MLBangBangTournamentChat"):
        assert key in text
    assert "BOT_TOKEN=\n" in text and ":PASSWORD@" in text


def test_sql_is_parametrised_in_repositories():
    """No f-string / % / .format SQL in the repositories (the single dynamic query is whitelisted by audience)."""
    for path in (ROOT / "app" / "repositories").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r'f"""?\s*(SELECT|INSERT|UPDATE|DELETE)', src, re.I), path
        assert ".format(" not in src and not re.search(r'"\s*%\s*\(', src), path


pytestmark_db = pytest.mark.db


@pytest.mark.db
async def test_sql_injection_payloads_are_stored_literally_or_rejected(pool):
    svc = TeamService(pool)
    evil = "x'); DROP TABLE teams;--"
    v = await svc.create_team(TgUser(1, "capone01", "Cap'; DROP TABLE users;--"), evil)
    assert v.name == evil
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT name FROM teams") == evil
        assert await c.fetchval("SELECT full_name FROM users") == "Cap'; DROP TABLE users;--"
    from app.services.errors import InvalidInvite

    for bad in ["' OR '1'='1' --" + "a" * 20, "a'; DROP TABLE teams;--" + "b" * 20, "x" * 32 + "' OR 1=1"]:
        with pytest.raises(InvalidInvite):
            await svc.join_team(TgUser(2, None, "X"), bad)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == 1

@pytest.mark.db
async def test_traceback_and_secrets_never_reach_user(pool, monkeypatch):
    h = Harness(pool)
    try:
        async def boom(*a, **k):
            raise RuntimeError("postgresql://mlbb_bot:HUNTER2@127.0.0.1/db")

        monkeypatch.setattr(h.svc.reg, "my_registration", boom)
        h.clear()
        await h.say(5, "newuser1", "/start")
        await h.press(5, "newuser1", "me")
        shown = " ".join(h.texts()) + " ".join(a.text or "" for a in h.answers())
        assert "HUNTER2" not in shown and "Traceback" not in shown and "RuntimeError" not in shown
        assert h.errors  # ...but it was logged for the operator
    finally:
        h.close()


@pytest.mark.db
async def test_admin_identity_only_from_telegram_sender(pool):
    """Even with a forged admin-looking username/full name the sender id decides."""
    h = Harness(pool)
    try:
        await make_team(pool, 1)
        h.clear()
        await h.press(ADMIN_ID + 1, "admin", "adm:t:0")
        await h.say(ADMIN_ID + 1, "admin", "/admin")
        assert "Всего" not in " ".join(h.texts()) and "Админ-панель" not in " ".join(h.texts())
        await h.press(ADMIN_ID, "whatever", "adm:t:0")
        assert "Всего: <b>1</b>" in h.last()
    finally:
        h.close()
