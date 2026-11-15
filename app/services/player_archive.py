"""Reserve archive of registered players.

Lives in its OWN SQLite file (player_archive.db next to the main database), so wiping, recreating or
deleting tournament.db does not lose it. Keyed by Telegram user_id. When a known player registers
again the bot pre-fills their data and they only adjust roles / stars.

Every method is best-effort: an archive failure is logged and never breaks a registration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite

log = logging.getLogger(__name__)

ARCHIVE_FILENAME = "player_archive.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    full_name    TEXT NOT NULL DEFAULT '',
    roles        TEXT,              -- comma-separated solo roles: EXP,GOLD,...
    stars        INTEGER,           -- last stars / max rank the player entered
    kind         TEXT,              -- last registration: solo | captain | member | registrar
    position     TEXT,              -- registrar position (Менеджер, Тренер...)
    team_name    TEXT,              -- last team the player created / joined
    reg_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


@dataclass(frozen=True)
class ArchivedPlayer:
    user_id: int
    username: Optional[str]
    full_name: str
    roles: list[str]
    stars: Optional[int]
    kind: Optional[str]
    position: Optional[str]
    team_name: Optional[str]
    reg_count: int


def archive_path_for(database_url: str) -> str:
    """player_archive.db in the same folder as the main SQLite database (project root for non-SQLite URLs)."""
    path = database_url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if path.startswith(prefix):
            return str(Path(path[len(prefix):]).resolve().with_name(ARCHIVE_FILENAME))
    if "://" not in path and path:
        return str(Path(path).resolve().with_name(ARCHIVE_FILENAME))
    from app.config import PROJECT_ROOT
    return str(PROJECT_ROOT / ARCHIVE_FILENAME)


class PlayerArchive:
    def __init__(self, path: str):
        self.path = path
        self._ready = False

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        if not self._ready:
            await conn.execute(_SCHEMA)
            await conn.commit()
            self._ready = True
        return conn

    async def get(self, user_id: int) -> Optional[ArchivedPlayer]:
        try:
            conn = await self._connect()
            try:
                cur = await conn.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
                row = await cur.fetchone()
            finally:
                await conn.close()
        except Exception:
            log.exception("player archive: read failed for %s", user_id)
            return None
        if row is None:
            return None
        roles = [r for r in (row["roles"] or "").split(",") if r]
        return ArchivedPlayer(
            user_id=row["user_id"], username=row["username"], full_name=row["full_name"] or "",
            roles=roles, stars=row["stars"], kind=row["kind"], position=row["position"],
            team_name=row["team_name"], reg_count=row["reg_count"] or 0,
        )

    async def save(
        self, user_id: int, username: Optional[str], full_name: str, *, kind: str,
        roles: Optional[str] = None, stars: Optional[int] = None,
        position: Optional[str] = None, team_name: Optional[str] = None,
    ) -> None:
        """Upsert after a successful registration. None fields keep the previously archived value."""
        try:
            conn = await self._connect()
            try:
                await conn.execute(
                    """
                    INSERT INTO players (user_id, username, full_name, roles, stars, kind, position, team_name, reg_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username   = excluded.username,
                        full_name  = excluded.full_name,
                        roles      = COALESCE(excluded.roles, players.roles),
                        stars      = COALESCE(excluded.stars, players.stars),
                        kind       = excluded.kind,
                        position   = COALESCE(excluded.position, players.position),
                        team_name  = COALESCE(excluded.team_name, players.team_name),
                        reg_count  = players.reg_count + 1,
                        updated_at = datetime('now')
                    """,
                    (user_id, username, (full_name or "")[:200], roles, stars, kind, position, team_name),
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception:
            log.exception("player archive: save failed for %s", user_id)

    async def count(self) -> int:
        try:
            conn = await self._connect()
            try:
                cur = await conn.execute("SELECT count(*) FROM players")
                return (await cur.fetchone())[0]
            finally:
                await conn.close()
        except Exception:
            log.exception("player archive: count failed")
            return 0

    async def import_from_db(self, pool) -> int:
        """Copy every player currently in the main DB into the archive (only users not archived yet).
        Run at startup so players registered before the archive existed are covered too."""
        try:
            async with pool.acquire() as db:
                solo = await db.fetch(
                    "SELECT s.user_id, u.username, u.full_name, s.role, s.stars "
                    "FROM solo_players s JOIN users u ON u.telegram_id = s.user_id"
                )
                members = await db.fetch(
                    "SELECT m.user_id, u.username, u.full_name, m.rank, m.is_captain, t.name AS team_name "
                    "FROM team_members m JOIN users u ON u.telegram_id = m.user_id JOIN teams t ON t.id = m.team_id"
                )
        except Exception:
            log.exception("player archive: reading main DB failed")
            return 0
        rows = [
            (r["user_id"], r["username"], r["full_name"] or "", r["role"], r["stars"], "solo", None)
            for r in solo
        ] + [
            (r["user_id"], r["username"], r["full_name"] or "", None, r["rank"],
             "captain" if r["is_captain"] else "member", r["team_name"])
            for r in members
        ]
        if not rows:
            return 0
        try:
            conn = await self._connect()
            try:
                before = conn.total_changes
                await conn.executemany(
                    "INSERT OR IGNORE INTO players (user_id, username, full_name, roles, stars, kind, team_name, reg_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    rows,
                )
                await conn.commit()
                return conn.total_changes - before
            finally:
                await conn.close()
        except Exception:
            log.exception("player archive: import failed")
            return 0
