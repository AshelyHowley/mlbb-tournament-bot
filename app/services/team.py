"""Team lifecycle. Three independent roles: registrar (created_by) / captain / member.
Identity is always the Telegram user_id. The registrar need not play; the captain always plays (counts toward 5)."""
from __future__ import annotations
import sqlite3

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.repositories import settings as settings_repo
from app.repositories import solo as solo_repo
from app.repositories import teams as teams_repo
from app.repositories import tournaments as tournaments_repo
from app.repositories import users as users_repo
from app.utils.audit import log_registration, log_action
from app.services.errors import (
    AlreadyInOtherTeam,
    AlreadyRegistered,
    CaptainExists,
    CaptainRequired,
    Conflict,
    InvalidInvite,
    NotCaptain,
    NotFound,
    RegistrationClosed,
    TeamFull,
    TeamNameTaken,
    ValidationError,
)
from app.services.registration import TgUser
from app.utils.text import INVITE_TOKEN_RE, clean_team_name, new_invite_token, person

log = logging.getLogger(__name__)

MAX_TEAM_SIZE = 5  # captain included; a registrar who does not play is NOT counted
CREATOR_ROLES = ("captain", "member", "registrar")
_TOKEN_RETRIES = 3

# sqlstates raised by our own triggers (see migrations 002/003)
_SOLO_CONFLICT, _TEAM_FULL = "MB001", "MB003"


@dataclass(frozen=True)
class Member:
    user_id: int
    username: Optional[str]
    full_name: str
    is_captain: bool

    @property
    def display(self) -> str:
        return person(self.full_name, self.username, self.user_id)


@dataclass(frozen=True)
class TeamView:
    team_id: int
    name: str
    invite_token: str                       # player invite
    captain_invite_token: Optional[str]     # captain invite (kept after use, but dead once a captain exists)
    captain_id: Optional[int]
    created_by: int
    creator_name: str
    creator_username: Optional[str]
    created_at: datetime
    members: tuple[Member, ...]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def free(self) -> int:
        return max(MAX_TEAM_SIZE - self.size, 0)

    @property
    def captain(self) -> Optional[Member]:
        return next((m for m in self.members if m.is_captain), None)

    @property
    def captain_pending(self) -> bool:
        return self.captain_id is None

    @property
    def registrar_display(self) -> str:
        return person(self.creator_name, self.creator_username, self.created_by)

    def role_of(self, user_id: int) -> Optional[str]:
        """The user's strongest role here: captain > member > registrar."""
        if self.captain_id == user_id:
            return "captain"
        if any(m.user_id == user_id for m in self.members):
            return "member"
        if self.created_by == user_id:
            return "registrar"
        return None


@dataclass(frozen=True)
class InvitePeek:
    """Read-only classification of an invite for the invitation screen (the authoritative check is in the join/accept call)."""
    status: str  # ok | full | mine | other | solo | closed | invalid | no_captain | captain_exists | mine_captain
    team: Optional[TeamView] = None


@dataclass(frozen=True)
class JoinResult:
    team: TeamView
    already_member: bool


@dataclass(frozen=True)
class CaptainResult:
    team: TeamView
    already_captain: bool
    was_member: bool  # True when an existing member was promoted (no new seat used)


async def _view(conn, team_row) -> TeamView:
    rows = await teams_repo.members(conn, team_row["id"])
    return TeamView(
        team_id=team_row["id"], name=team_row["name"], invite_token=team_row["invite_token"],
        captain_invite_token=team_row["captain_invite_token"], captain_id=team_row["captain_id"],
        created_by=team_row["created_by"], creator_name=team_row["creator_name"] or "",
        creator_username=team_row["creator_username"], created_at=team_row["created_at"],
        members=tuple(Member(r["user_id"], r["username"], r["full_name"] or "", r["is_captain"]) for r in rows),
    )


def _map_pg(e: asyncpg.PostgresError) -> None:
    """Translate our trigger errors into domain errors (raises), otherwise return."""
    if e.sqlstate == _TEAM_FULL:
        raise TeamFull()
    if e.sqlstate == _SOLO_CONFLICT:
        raise AlreadyRegistered("ℹ️ Вы зарегистрированы как соло-игрок — вступить в команду нельзя.")


class TeamService:
    def __init__(self, pool: object):
        self.pool = pool

    async def _is_open(self, conn) -> bool:
        return (await settings_repo.get(conn, "registration_open", "true")) == "true"

    # ---- queries ----------------------------------------------------------------------------
    async def view_for_user(self, user_id: int) -> Optional[TeamView]:
        """The team the user plays for; else the team they registered (registrar-only)."""
        async with self.pool.acquire() as conn:
            m = await teams_repo.membership(conn, user_id)
            team = await teams_repo.get(conn, m["team_id"]) if m else await teams_repo.get_created_by(conn, user_id)
            return await _view(conn, team) if team else None

    async def registrar_view(self, user_id: int) -> Optional[TeamView]:
        """The team this user registered (regardless of whether they also play elsewhere)."""
        async with self.pool.acquire() as conn:
            team = await teams_repo.get_created_by(conn, user_id)
            return await _view(conn, team) if team else None

    async def name_taken(self, name_norm: str) -> bool:
        async with self.pool.acquire() as conn:
            return await teams_repo.get_by_name_norm(conn, name_norm) is not None

    async def peek_invite(self, user_id: int, token: str) -> InvitePeek:
        """Player invite."""
        if not INVITE_TOKEN_RE.fullmatch(token or ""):
            return InvitePeek("invalid")
        async with self.pool.acquire() as conn:
            team = await teams_repo.get_by_token(conn, token)
            if not team or team["tournament_id"] != await tournaments_repo.active_id(conn):
                return InvitePeek("invalid")  # a dead link from an archived tournament looks like any invalid link
            view = await _view(conn, team)
            mine = await teams_repo.membership(conn, user_id)
            if mine:
                return InvitePeek("mine" if mine["team_id"] == team["id"] else "other", view)
            if await solo_repo.get(conn, user_id):
                return InvitePeek("solo", view)
            if team["captain_id"] is None:
                return InvitePeek("no_captain", view)
            if not await self._is_open(conn):
                return InvitePeek("closed", view)
            return InvitePeek("full" if view.size >= MAX_TEAM_SIZE else "ok", view)

    async def peek_captain_invite(self, user_id: int, token: str) -> InvitePeek:
        if not INVITE_TOKEN_RE.fullmatch(token or ""):
            return InvitePeek("invalid")
        async with self.pool.acquire() as conn:
            team = await teams_repo.get_by_captain_token(conn, token)
            if not team or team["tournament_id"] != await tournaments_repo.active_id(conn):
                return InvitePeek("invalid")
            view = await _view(conn, team)
            if team["captain_id"] is not None:
                return InvitePeek("mine_captain" if team["captain_id"] == user_id else "captain_exists", view)
            mine = await teams_repo.membership(conn, user_id)
            if mine:
                return InvitePeek("ok" if mine["team_id"] == team["id"] else "other", view)  # member -> promotion
            if await solo_repo.get(conn, user_id):
                return InvitePeek("solo", view)
            if not await self._is_open(conn):
                return InvitePeek("closed", view)
            return InvitePeek("full" if view.size >= MAX_TEAM_SIZE else "ok", view)

    # ---- create -----------------------------------------------------------------------------
    async def create_team(self, user: TgUser, name_raw: str, role: str = "captain", *, is_admin: bool = False) -> TeamView:
        """role: 'captain' (creator captains and plays), 'member' (creator plays, captain comes later),
        'registrar' (creator only registers, does not play). An ordinary user may register one team; the admin any number."""
        if role not in CREATOR_ROLES:
            raise ValidationError("❌ Неизвестная роль.")
        named = clean_team_name(name_raw)
        if named is None:
            raise ValidationError("❌ Название команды: от 2 до 40 символов, без служебных символов.")
        name, name_norm = named
        for attempt in range(_TOKEN_RETRIES):
            try:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await users_repo.upsert_user(conn, user.id, user.username, user.full_name)
                        if not await self._is_open(conn):
                            raise RegistrationClosed()
                        await teams_repo.lock_user(conn, user.id)
                        # Only block if currently a member (created_count alone no longer blocks)
                        if not is_admin and await teams_repo.membership(conn, user.id):
                            raise AlreadyRegistered("ℹ️ Вы уже состоите в команде.")
                        if role != "registrar":  # the creator will occupy a player slot
                            if await teams_repo.membership(conn, user.id):
                                raise AlreadyRegistered("ℹ️ Вы уже состоите в команде.")
                            if await solo_repo.get(conn, user.id):
                                raise AlreadyRegistered("ℹ️ Вы уже зарегистрированы как соло-игрок.")
                        if await teams_repo.get_by_name_norm(conn, name_norm):
                            raise TeamNameTaken()
                        is_captain = role == "captain"
                        team_id = await teams_repo.insert_team(
                            conn, name, name_norm, user.id, user.id if is_captain else None,
                            new_invite_token(), None if is_captain else new_invite_token(),
                        )
                        if role != "registrar":
                            await teams_repo.add_member(conn, team_id, user.id, is_captain=is_captain)
                        await log_registration(conn, user.id, 'create_team', name, f'role={role}')
                        await log_action(conn, user.id, user.username, 'create_team', name)
                        return await _view(conn, await teams_repo.get(conn, team_id))
            except sqlite3.IntegrityError as e:
                c = str(e) or ""
                if c in ("teams_invite_token_uq", "teams_captain_invite_token_uq") and attempt < _TOKEN_RETRIES - 1:
                    continue  # astronomically unlikely token collision: draw new ones
                if c == "teams_name_norm_uq":
                    raise TeamNameTaken()
                raise AlreadyRegistered("ℹ️ Вы уже зарегистрированы.")
            except sqlite3.IntegrityError as e:
                _map_pg(e)
                raise
        raise Conflict("⚠️ Сервер занят, повторите попытку.")  # pragma: no cover

    # ---- join as a player -------------------------------------------------------------------
    async def join_team(self, user: TgUser, token: str) -> JoinResult:
        if not INVITE_TOKEN_RE.fullmatch(token or ""):
            raise InvalidInvite()
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await users_repo.upsert_user(conn, user.id, user.username, user.full_name)
                    await teams_repo.lock_user(conn, user.id)
                    # the team row lock serialises every roster change of this team (join / captain / remove / transfer)
                    team = await teams_repo.get_by_token(conn, token, lock=True)
                    if not team or team["tournament_id"] != await tournaments_repo.active_id(conn):
                        raise InvalidInvite()
                    mine = await teams_repo.membership(conn, user.id)
                    if mine:
                        if mine["team_id"] == team["id"]:
                            return JoinResult(await _view(conn, team), already_member=True)
                        raise AlreadyInOtherTeam()
                    if await solo_repo.get(conn, user.id):
                        raise AlreadyRegistered("ℹ️ Вы зарегистрированы как соло-игрок — вступить в команду нельзя.")
                    if team["captain_id"] is None:
                        raise CaptainRequired()
                    if not await self._is_open(conn):
                        raise RegistrationClosed()
                    if await teams_repo.member_count(conn, team["id"]) >= MAX_TEAM_SIZE:
                        raise TeamFull()
                    await teams_repo.add_member(conn, team["id"], user.id)
                return JoinResult(await _view(conn, team), already_member=False)
        except sqlite3.IntegrityError:
            raise AlreadyInOtherTeam()
        except sqlite3.IntegrityError as e:
            _map_pg(e)
            raise

    # ---- captain invite ---------------------------------------------------------------------
    async def accept_captain(self, user: TgUser, token: str) -> CaptainResult:
        """Atomically: validate the captain token, make the user a member (unless already one) and the captain."""
        if not INVITE_TOKEN_RE.fullmatch(token or ""):
            raise InvalidInvite()
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await users_repo.upsert_user(conn, user.id, user.username, user.full_name)
                    await teams_repo.lock_user(conn, user.id)
                    team = await teams_repo.get_by_captain_token(conn, token, lock=True)
                    if not team or team["tournament_id"] != await tournaments_repo.active_id(conn):
                        raise InvalidInvite()  # unknown token, dead (archived tournament), or replaced by "refresh link"
                    if team["captain_id"] is not None:
                        if team["captain_id"] == user.id:
                            return CaptainResult(await _view(conn, team), already_captain=True, was_member=True)
                        raise CaptainExists()
                    mine = await teams_repo.membership(conn, user.id)
                    if mine and mine["team_id"] != team["id"]:
                        raise AlreadyInOtherTeam()
                    if mine:  # already a member of this team: promote, no second membership, no new seat
                        was_member = True
                        await teams_repo.assign_captain(conn, team["id"], user.id)
                        await teams_repo.flag_member_captain(conn, team["id"], user.id, True)
                    else:
                        was_member = False
                        if await solo_repo.get(conn, user.id):
                            raise AlreadyRegistered("ℹ️ Вы зарегистрированы как соло-игрок — стать капитаном нельзя.")
                        if not await self._is_open(conn):
                            raise RegistrationClosed()
                        if await teams_repo.member_count(conn, team["id"]) >= MAX_TEAM_SIZE:
                            raise TeamFull()
                        await teams_repo.assign_captain(conn, team["id"], user.id)
                        await teams_repo.add_member(conn, team["id"], user.id, is_captain=True)
                fresh = await teams_repo.get(conn, team["id"])  # re-read: captain_id changed inside the tx
                return CaptainResult(await _view(conn, fresh), already_captain=False, was_member=was_member)
        except sqlite3.IntegrityError:
            raise AlreadyInOtherTeam()
        except sqlite3.IntegrityError as e:
            _map_pg(e)
            raise

    async def regenerate_captain_token(self, registrar_id: int) -> TeamView:
        """Registrar replaces the captain link while no captain is assigned; the old link dies immediately."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                team = await teams_repo.get_created_by(conn, registrar_id, lock=True)
                if not team:
                    raise NotFound("Регистрация")
                if team["captain_id"] is not None:
                    raise CaptainExists()
                for _ in range(_TOKEN_RETRIES):
                    try:
                        async with conn.transaction():  # savepoint: a token collision must not poison the outer tx
                            await teams_repo.set_captain_token(conn, team["id"], new_invite_token())
                        break
                    except sqlite3.IntegrityError:
                        continue
                team = await teams_repo.get(conn, team["id"])
            return await _view(conn, team)

    async def cancel_registration(self, registrar_id: int) -> str:
        """Registrar cancels their team while no captain is assigned. Returns the team name."""
        async with self.pool.acquire() as conn:
            try:
                async with conn.transaction():
                    team = await teams_repo.get_created_by(conn, registrar_id, lock=True)
                    if not team:
                        raise NotFound("Регистрация")
                    if team["captain_id"] is not None:
                        raise Conflict("⛔ Капитан уже назначен — управление командой перешло к нему. Для отмены обратитесь к администратору.")
                    await teams_repo.delete(conn, team["id"])
                    return team["name"]
            except sqlite3.IntegrityError:
                raise Conflict("❌ Регистрацию нельзя отменить: команда участвует в матчах. Обратитесь к администратору.")

    # ---- roster management (captain only) ----------------------------------------------------
    async def remove_member(self, captain_id: int, target_id: int) -> tuple[Member, TeamView]:
        """Captain removes another member. Returns (removed member, updated team)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                team = await teams_repo.get_by_captain(conn, captain_id, lock=True)
                if not team:
                    raise NotCaptain()
                if target_id == captain_id:
                    raise Conflict("⛔ Капитана нельзя удалить из команды.")
                before = await _view(conn, team)
                target = next((m for m in before.members if m.user_id == target_id), None)
                if target is None or not await teams_repo.remove_member(conn, team["id"], target_id):
                    raise NotFound("Игрок")
            return target, await _view(conn, team)

    async def transfer_captaincy(self, captain_id: int, new_captain_id: int) -> tuple[Member, Member, TeamView]:
        """Current captain hands the role to an existing member of the same team. Atomic; size does not change.
        Returns (new captain, old captain, updated team)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                team = await teams_repo.get_by_captain(conn, captain_id, lock=True)
                if not team:
                    raise NotCaptain()
                if new_captain_id == captain_id:
                    raise Conflict("ℹ️ Вы уже капитан этой команды.")
                before = await _view(conn, team)
                new = next((m for m in before.members if m.user_id == new_captain_id and not m.is_captain), None)
                old = before.captain
                if new is None or old is None:
                    raise NotFound("Игрок")  # not a member of THIS team (forged id / already removed)
                if not await teams_repo.swap_captain(conn, team["id"], captain_id, new_captain_id):
                    raise Conflict("⚠️ Не удалось передать капитанство, повторите.")
                team = await teams_repo.get(conn, team["id"])
            return new, old, await _view(conn, team)

    async def leave_team(self, user_id: int) -> tuple[Member, TeamView]:
        """A regular member leaves. The captain cannot: transfer the captaincy first (or ask the administrator)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                mine = await teams_repo.membership(conn, user_id)
                if not mine:
                    raise NotFound("Команда")
                if mine["is_captain"]:
                    raise Conflict(
                        "⛔ Капитан не может покинуть команду. Передайте капитанство другому участнику "
                        "или обратитесь к администратору."
                    )
                team = await conn.fetchrow(
                    "SELECT id FROM teams WHERE id = $1", mine["team_id"]
                )
                team = await teams_repo.get(conn, team["id"])
                before = await _view(conn, team)
                me = next((m for m in before.members if m.user_id == user_id), None)
                if me is None or not await teams_repo.remove_member(conn, team["id"], user_id):
                    raise NotFound("Команда")
                # Fully release the user: clear creator flag and any solo registration
                await conn.execute(
                    "UPDATE teams SET created_by = NULL WHERE created_by = $1 AND tournament_id = (SELECT id FROM tournaments WHERE status = 'active')",
                    user_id,
                )
                await conn.execute(
                    "DELETE FROM solo_players WHERE user_id = $1 AND tournament_id = (SELECT id FROM tournaments WHERE status = 'active')",
                    user_id,
                )
            return me, await _view(conn, team)
