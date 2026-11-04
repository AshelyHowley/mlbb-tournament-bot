"""SQLite connection pool that emulates the asyncpg interface used by the bot.

Provides: acquire(), execute(), fetch(), fetchrow(), fetchval(), transaction(), close().
Converts $1, $2, ... placeholders to ? and returns dict-like records.
"""
from __future__ import annotations

import logging
import re
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

    async def execute(self, sql: str, *args: Any) -> str:
        sql, args = _convert_placeholders(sql, args)
        cur = await self._conn.execute(sql, args)
        # Commit only if not inside an explicit transaction we started
        if not self._in_tx:
            await self._conn.commit()
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
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        return [Record(dict(r)) for r in rows]

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
        self._in_tx = True
        await self._conn.execute("BEGIN")
        try:
            yield self
            await self._conn.commit()
        except Exception:
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
        raw = await aiosqlite.connect(self.pool.path)
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
