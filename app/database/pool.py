"""SQLite connection pool that emulates the asyncpg interface used by the bot.

Provides: acquire(), execute(), fetch(), fetchrow(), fetchval(), transaction(), close().
Converts $1, $2, ... placeholders to ? and returns dict-like records.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Optional, Sequence

import aiosqlite

log = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\$(\d+)")


def _convert_placeholders(sql: str, args: tuple) -> tuple[str, tuple]:
    """Convert $1, $2... to ? and expand args so repeated $N are duplicated."""
    indices = [int(m.group(1)) - 1 for m in _PLACEHOLDER.finditer(sql)]
    new_sql = _PLACEHOLDER.sub("?", sql)
    if not indices:
        return new_sql, args
    new_args = tuple(args[i] if i < len(args) else None for i in indices)
    return new_sql, new_args


# '<sqlstate>:<constraint>: text' raised by our triggers (migration 019)
_TRIGGER_ERR = re.compile(r"^(MB\d{3}|\d{5}):([a-z0-9_]+):")
# native UNIQUE failures name columns, not constraints: map them to the PostgreSQL-era names
_UNIQUE_COLUMNS = {
    "teams.name_norm, teams.tournament_id": "teams_name_norm_uq",
    "solo_players.user_id": "solo_players_pkey",
    "team_members.team_id, team_members.user_id": "team_members_team_user_uq",
    # partial unique indexes from migration 019 are reported by column too
    "team_members.team_id": "team_members_one_captain_uq",
    "teams.captain_id, teams.tournament_id": "teams_captain_uq",
    "teams.invite_token": "teams_invite_token_uq",
    "teams.captain_invite_token": "teams_captain_invite_token_uq",
    "matches.tournament_id, matches.stage_code": "matches_one_per_stage_uq",
}
_NATIVE_STATES = (
    ("UNIQUE constraint failed", "23505"),
    ("CHECK constraint failed", "23514"),
    ("FOREIGN KEY constraint failed", "23503"),
    ("NOT NULL constraint failed", "23502"),
)


def _annotate(e: sqlite3.DatabaseError) -> None:
    """Give a sqlite3 error the sqlstate / constraint_name attributes the services expect (as asyncpg had)."""
    msg = str(e)
    m = _TRIGGER_ERR.match(msg)
    if m:
        e.sqlstate, e.constraint_name = m.group(1), m.group(2)
        return
    e.sqlstate, e.constraint_name = None, None
    for prefix, state in _NATIVE_STATES:
        if msg.startswith(prefix):
            e.sqlstate = state
            detail = msg[len(prefix):].lstrip(": ")
            if detail.startswith("index '"):
                e.constraint_name = detail[len("index '"):].rstrip("'")
            else:
                e.constraint_name = _UNIQUE_COLUMNS.get(detail)
            return


class Record(dict):
    """dict that also supports attribute and index access like dict."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class Connection:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        self._conn.row_factory = aiosqlite.Row
        self._in_tx = False
        self._sp = 0

    async def execute(self, sql: str, *args: Any) -> str:
        sql, args = _convert_placeholders(sql, args)
        try:
            cur = await self._conn.execute(sql, args)
            # Commit only if not inside an explicit transaction we started
            if not self._in_tx:
                await self._conn.commit()
        except sqlite3.DatabaseError as e:
            await self._failed(e)
            raise
        # Mimic asyncpg status strings roughly
        if sql.strip().upper().startswith("INSERT"):
            return f"INSERT 0 {cur.lastrowid or 0}"
        if sql.strip().upper().startswith("UPDATE"):
            return f"UPDATE {cur.rowcount}"
        if sql.strip().upper().startswith("DELETE"):
            return f"DELETE {cur.rowcount}"
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Record]:
        sql, args = _convert_placeholders(sql, args)
        try:
            cur = await self._conn.execute(sql, args)
            rows = await cur.fetchall()
            # INSERT/UPDATE ... RETURNING goes through fetch*: without this commit it was rolled back on close
            if not self._in_tx and self._conn.in_transaction:
                await self._conn.commit()
        except sqlite3.DatabaseError as e:
            await self._failed(e)
            raise
        return [Record(dict(r)) for r in rows]

    async def _failed(self, e: sqlite3.DatabaseError) -> None:
        _annotate(e)
        # outside transaction() a failed write must not leave sqlite3's implicit transaction open,
        # or the next transaction() on this connection fails with "cannot start a transaction"
        if not self._in_tx and self._conn.in_transaction:
            await self._conn.rollback()

    async def fetchrow(self, sql: str, *args: Any) -> Optional[Record]:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        if row is None:
            return None
        return next(iter(row.values()))

    @asynccontextmanager
    async def transaction(self):
        if self._in_tx:
            # nested transaction() = SAVEPOINT, like asyncpg: an inner failure rolls back only the inner part
            self._sp += 1
            name = f"sp_{self._sp}"
            await self._conn.execute(f"SAVEPOINT {name}")
            try:
                yield self
                await self._conn.execute(f"RELEASE {name}")
            except BaseException:
                await self._conn.execute(f"ROLLBACK TO {name}")
                await self._conn.execute(f"RELEASE {name}")
                raise
            return
        self._in_tx = True
        await self._conn.execute("BEGIN IMMEDIATE")  # take the write lock up front: no deadlock on lock upgrade
        try:
            yield self
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
        finally:
            self._in_tx = False

    async def close(self) -> None:
        await self._conn.close()


class _AcquireContext:
    """Supports both `conn = await pool.acquire()` and `async with pool.acquire() as conn`."""

    def __init__(self, pool: "Pool"):
        self.pool = pool
        self.conn: Connection | None = None

    def __await__(self):
        return self._acquire().__await__()

    async def _acquire(self) -> Connection:
        raw = await aiosqlite.connect(self.pool.path, timeout=30)  # wait for a busy writer instead of failing
        await raw.execute("PRAGMA foreign_keys = ON")
        await raw.execute("PRAGMA journal_mode = WAL")
        self.conn = Connection(raw)
        return self.conn

    async def __aenter__(self) -> Connection:
        if self.conn is None:
            await self._acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn is not None:
            await self.conn.close()
            self.conn = None


class Pool:
    """Minimal pool — SQLite is file-based, one connection is usually enough."""

    def __init__(self, path: str):
        self.path = path

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self)

    # one-shot helpers like asyncpg.Pool: each call runs on its own short-lived connection
    async def execute(self, sql: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Record]:
        async with self.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[Record]:
        async with self.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def close(self) -> None:
        pass  # connections are closed by the caller


async def create_pool(dsn: str, **_: Any) -> Pool:
    """dsn: sqlite+aiosqlite:///./tournament.db or sqlite:///path"""
    path = dsn
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if not path:
        raise ValueError("Empty SQLite path in DATABASE_URL")
    log.info("SQLite pool created: %s", path)
    return Pool(path)


class WrongDatabaseError(Exception):
    pass


async def verify_identity(conn: Connection, *args, **kwargs) -> None:
    """No-op for SQLite."""
    return None
