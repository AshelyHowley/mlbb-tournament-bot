"""FSM storage that survives restarts: dialog state lives in its own SQLite file (fsm.db).

Kept apart from tournament.db on purpose: backups/restores of the tournament database must never
bring back half-filled questionnaires, and the tournament schema stays untouched.
A state nobody touched for ``ttl`` seconds is forgotten, so a long-abandoned dialog doesn't trap the user.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from app.services.player_archive import archive_path_for

log = logging.getLogger(__name__)

FSM_FILENAME = "fsm.db"
DEFAULT_TTL = 24 * 3600


def fsm_path_for(database_url: str) -> str:
    """fsm.db in the same folder as the main SQLite database (so on Railway it sits on the Volume)."""
    return str(Path(archive_path_for(database_url)).with_name(FSM_FILENAME))


def _key(k: StorageKey) -> str:
    return f"{k.bot_id}:{k.chat_id}:{k.user_id}:{k.thread_id or ''}:{k.business_connection_id or ''}:{k.destiny}"


class SQLiteStorage(BaseStorage):
    def __init__(self, path: str, ttl: Optional[float] = DEFAULT_TTL, clock=time.time):
        self.path = path
        self.ttl = ttl
        self._clock = clock
        self._db: Optional[aiosqlite.Connection] = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            db = await aiosqlite.connect(self.path)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS fsm (key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT '{}', "
                "updated REAL NOT NULL)"
            )
            if self.ttl:
                await db.execute("DELETE FROM fsm WHERE updated < ?", (self._clock() - self.ttl,))
            await db.commit()
            self._db = db
        return self._db

    async def _row(self, key: StorageKey) -> tuple[Optional[str], dict[str, Any]]:
        db = await self._conn()
        async with db.execute("SELECT state, data, updated FROM fsm WHERE key = ?", (_key(key),)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None, {}
        state, data, updated = row
        if self.ttl and updated < self._clock() - self.ttl:
            await db.execute("DELETE FROM fsm WHERE key = ?", (_key(key),))
            await db.commit()
            return None, {}
        try:
            return state, json.loads(data)
        except ValueError:
            log.warning("corrupt FSM data for %s, dropping it", _key(key))
            return state, {}

    async def _write(self, key: StorageKey, state: Optional[str], data: Mapping[str, Any]) -> None:
        db = await self._conn()
        if state is None and not data:
            await db.execute("DELETE FROM fsm WHERE key = ?", (_key(key),))
        else:
            await db.execute(
                "INSERT INTO fsm (key, state, data, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET state = excluded.state, data = excluded.data, updated = excluded.updated",
                (_key(key), state, json.dumps(dict(data), ensure_ascii=False), self._clock()),
            )
        await db.commit()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        _, data = await self._row(key)
        await self._write(key, value, data)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return (await self._row(key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        state, _ = await self._row(key)
        await self._write(key, state, data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict((await self._row(key))[1])

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
