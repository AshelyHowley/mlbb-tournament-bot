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
