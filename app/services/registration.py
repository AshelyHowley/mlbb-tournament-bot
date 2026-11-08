"""Registration state, solo registration and the registration switch. Team lifecycle lives in team.py."""
from __future__ import annotations
import sqlite3

import logging
from dataclasses import dataclass
from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.repositories import settings as settings_repo
from app.repositories import solo as solo_repo
from app.repositories import teams as teams_repo
from app.repositories import users as users_repo
from app.utils.audit import log_registration, log_action
from app.services.errors import AlreadyRegistered, Conflict, RegistrationClosed, ValidationError
from app.utils.text import normalize_username

log = logging.getLogger(__name__)

ROLES = ("EXP", "GOLD", "MID", "JUNGLE", "ROAM")
_DEADLOCK_RETRIES = 3

_SQLSTATE_TEAM_CONFLICT = "MB001"  # raised by our triggers: user is both solo and in a team


@dataclass(frozen=True)
class TgUser:
    id: int
    username: Optional[str]  # optional: Telegram accounts may have no username
    full_name: str = ""


@dataclass(frozen=True)
class MyRegistration:
    """What a Telegram user has registered. `kind` is the primary one: captain/member (plays for a team)
    > solo > registrar (only registered a team, does not play)."""
    kind: str  # 'captain' | 'member' | 'solo' | 'registrar'
    role: Optional[str] = None       # solo role (EXP/GOLD/...)
    username: Optional[str] = None
    full_name: str = ""
    team_role: Optional[str] = None  # 'captain' | 'member' | 'registrar' | None
    solo: bool = False
    stars: int = 0

    @property
    def plays(self) -> bool:
        """Occupies a player slot somewhere (team member or solo). A registrar-only user does not."""
        return self.team_role in ("captain", "member") or self.solo

    @property
    def has_team(self) -> bool:
        return self.team_role is not None


class RegistrationService:
    def __init__(self, pool: object):
        self.pool = pool

    async def is_open(self, conn: Optional[asyncpg.Connection] = None) -> bool:
        if conn is None:
            async with self.pool.acquire() as c:
                return await self.is_open(c)
        return (await settings_repo.get(conn, "registration_open", "true")) == "true"

    async def set_open(self, value: bool) -> None:
        async with self.pool.acquire() as conn:
            await settings_repo.set_(conn, "registration_open", "true" if value else "false")

    async def my_registration(self, user_id: int) -> Optional[MyRegistration]:
        """Everything is keyed by Telegram user_id; usernames play no role."""
        async with self.pool.acquire() as conn:
            m = await teams_repo.membership(conn, user_id)
            solo = await solo_repo.get(conn, user_id)
            team_role = None
            if m:
                team_role = "captain" if m["is_captain"] else "member"
            # created_count no longer marks you as registered once you left
        if team_role is None and solo is None:
            return None
        kind = team_role if team_role in ("captain", "member") else ("solo" if solo else "registrar")
        return MyRegistration(
            kind=kind, role=solo["role"] if solo else None, username=solo["username"] if solo else None,
            full_name=(solo["full_name"] or "") if solo else "", team_role=team_role, solo=solo is not None,
            stars=solo["stars"] if solo and "stars" in solo.keys() else 0,
        )

    async def register_solo(self, user: TgUser, role: str, stars: int = 0) -> MyRegistration:
        roles_list = [r.strip() for r in role.split(",")] if "," in role else [role]
        if not all(r in ROLES or r == "ALL" for r in roles_list):
            raise ValidationError("❌ Неизвестная роль.")
        norm = normalize_username(user.username)
        for attempt in range(_DEADLOCK_RETRIES):
            try:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await users_repo.upsert_user(conn, user.id, user.username, user.full_name)
                        if not await self.is_open(conn):
                            raise RegistrationClosed()
                        await teams_repo.lock_user(conn, user.id)
                        if await solo_repo.get(conn, user.id):
                            raise AlreadyRegistered("ℹ️ Вы уже зарегистрированы как соло-игрок.")
                        if await teams_repo.membership(conn, user.id):
                            raise AlreadyRegistered("ℹ️ Вы уже состоите в команде — соло-регистрация невозможна.")
                        await solo_repo.insert(conn, user.id, norm, norm, role, stars)
                        await log_registration(conn, user.id, 'solo', None, role)
                        await log_action(conn, user.id, user.username, 'solo_register', role)
                        return MyRegistration(kind="solo", role=role, username=norm, full_name=user.full_name, solo=True, stars=stars)
            except Exception:
                if attempt == _DEADLOCK_RETRIES - 1:
                    raise Conflict("⚠️ Сервер занят, повторите попытку.")
            except sqlite3.IntegrityError:
                raise AlreadyRegistered("ℹ️ Вы уже зарегистрированы.")
            except sqlite3.IntegrityError:
                raise AlreadyRegistered("ℹ️ Вы уже зарегистрированы.")
        raise Conflict("⚠️ Сервер занят, повторите попытку.")  # pragma: no cover
