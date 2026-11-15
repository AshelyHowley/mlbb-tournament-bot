from __future__ import annotations

from typing import Optional

from aiogram.methods import SendMessage
from app.services.registration import TgUser
from app.services.team import TeamService


def uname(i: int, prefix: str = "user") -> str:
    return f"{prefix}{i:04d}"


async def add_user(pool, uid: int, username: Optional[str]) -> None:
    from app.repositories import users

    async with pool.acquire() as c:
        await users.upsert_user(c, uid, username, f"User {uid}")


def captain_of(n: int) -> TgUser:
    return TgUser(id=n * 10, username=uname(n, "cap"), full_name=f"Captain {n}")


def member_of(n: int, k: int) -> TgUser:
    return TgUser(id=n * 10 + k, username=uname(n * 10 + k, "mem"), full_name=f"Member {n * 10 + k}")


async def make_team(pool, n: int, name: Optional[str] = None, size: int = 5):
    """Team #n: captain id=n*10 'capNNNN'; members ids n*10+1.. join through the invite token. Returns team id."""
    svc = TeamService(pool)
    view = await svc.create_team(captain_of(n), name or f"Team {n}")
    for k in range(1, size):
        await svc.join_team(member_of(n, k), view.invite_token)
    return view.team_id


async def team_token(pool, team_id: int) -> str:
    async with pool.acquire() as c:
        return await c.fetchval("SELECT invite_token FROM teams WHERE id = $1", team_id)


def dummy_method() -> SendMessage:
    return SendMessage(chat_id=1, text="x")


async def set_winner(pool, match_id: int, winner_id: int) -> None:
    """Decide a match exactly the way the bot does it (admin_panel `br:win`)."""
    from app.services.bracket_progress import record_winner
    async with pool.acquire() as c, c.transaction():
        await record_winner(c, match_id, winner_id)


async def ensure_third_place_stage(pool) -> None:
    """No-op since migration 020 adds the 'third' stage; kept for older tests."""
    async with pool.acquire() as c:
        await c.execute("INSERT OR IGNORE INTO stages (code, title, sort_order) VALUES ('third', '3 место', 45)")


async def ui_add_pair(h, actor_id: int, actor_uname: Optional[str], pool, a: int, b: int, stage: str) -> Optional[int]:
    """Add a bracket pair through the bot's own buttons (Сетка -> pick, pick, stage). Returns the match id."""
    await h.press(actor_id, actor_uname, f"br:pick:{a}")
    await h.press(actor_id, actor_uname, f"br:pick:{b}")
    await h.press(actor_id, actor_uname, f"br:stage:{stage}")
    async with pool.acquire() as c:
        return await c.fetchval(
            "SELECT id FROM matches WHERE stage_code = $1 AND team_a_id = $2 AND team_b_id = $3", stage, a, b)


async def ui_pick_winner(h, actor_id: int, actor_uname: Optional[str], match_id: int, winner: int) -> None:
    """Decide a pair through the bracket editor button (br:win)."""
    await h.press(actor_id, actor_uname, f"br:win:{match_id}:{winner}")
