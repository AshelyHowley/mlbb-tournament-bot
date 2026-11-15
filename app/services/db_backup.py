"""Резервная копия SQLite-базы для кнопки «💾 Скачать базу».

Копия делается через sqlite3 backup API, а не простым чтением файла: так она целостная
даже пока бот пишет в базу (WAL-режим, незакоммиченные транзакции в копию не попадают).
Функции синхронные - из async-кода вызывайте через asyncio.to_thread(...).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # бот не может отправить файл больше 50 МБ


def sqlite_path(database_url: str) -> Optional[str]:
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if database_url.startswith(prefix):
            return database_url[len(prefix):]
    return None


def snapshot(path: str) -> bytes:
    """Целостная копия базы по пути `path` в виде байтов файла .db."""
    if not Path(path).exists():
        raise FileNotFoundError(path)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------------------- sending
def _has_data(db_bytes: bytes) -> bool:
    """Пустая (только что созданная) база не стоит копии - её легко принять за настоящую."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        Path(tmp).write_bytes(db_bytes)
        c = sqlite3.connect(tmp)
        try:
            return bool(c.execute(
                "SELECT (SELECT count(*) FROM teams) + (SELECT count(*) FROM solo_players) "
                "     + (SELECT count(*) FROM tournaments WHERE status <> 'active')"
            ).fetchone()[0])
        except sqlite3.DatabaseError:
            return True  # неизвестная схема - лучше прислать, чем потерять
        finally:
            c.close()
    finally:
        os.unlink(tmp)


async def send_backup(bot, chat_id: int, cfg, archive_path: Optional[str], *, label: str = "",
                      silent: bool = False) -> list[tuple[str, bytes]]:
    """Делает копии tournament.db и player_archive.db и отправляет их `chat_id`.
    Возвращает отправленное [(имя, байты)]."""
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from aiogram.types import BufferedInputFile

    stamp = datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d_%H-%M")
    sent: list[tuple[str, bytes]] = []
    for name, path in (("tournament", sqlite_path(cfg.database_url)), ("player_archive", archive_path)):
        if not path:
            continue
        try:
            data = await asyncio.to_thread(snapshot, path)
        except (FileNotFoundError, sqlite3.DatabaseError):
            continue
        if len(data) > TELEGRAM_FILE_LIMIT:
            await bot.send_message(chat_id, f"⚠️ {name}.db больше 50 МБ - Telegram не даст его отправить.")
            continue
        await bot.send_document(
            chat_id, BufferedInputFile(data, filename=f"{name}_{stamp}.db"),
            caption=f"💾 {label}{name}.db · {stamp}", disable_notification=silent,
        )
        sent.append((name, data))
    return sent


class AutoBackup:
    """Раз в `every_hours` часов присылает главному админу копию базы - только если она изменилась
    с прошлой копии и в ней есть данные. Плюс попытка прислать копию при остановке бота (деплой)."""

    def __init__(self, bot, cfg, archive_path: Optional[str], every_hours: int):
        self.bot, self.cfg, self.archive_path = bot, cfg, archive_path
        self.every = max(int(every_hours), 0) * 3600
        self._last_hash: Optional[str] = None
        self._task = None

    async def run_once(self, label: str = "Автокопия: ") -> bool:
        import asyncio
        import hashlib

        path = sqlite_path(self.cfg.database_url)
        if not path:
            return False
        try:
            data = await asyncio.to_thread(snapshot, path)
        except (FileNotFoundError, sqlite3.DatabaseError):
            return False
        digest = hashlib.sha256(data).hexdigest()
        if digest == self._last_hash or not await asyncio.to_thread(_has_data, data):
            return False
        await send_backup(self.bot, self.cfg.admin_id, self.cfg, self.archive_path, label=label, silent=True)
        self._last_hash = digest
        return True

    def start(self) -> None:
        import asyncio
        if self.every and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        import asyncio
        import logging
        log = logging.getLogger(__name__)
        await asyncio.sleep(60)  # даём боту спокойно запуститься
        while True:
            try:
                if await self.run_once():
                    log.info("auto backup sent to the main admin")
            except asyncio.CancelledError:
                raise
            except Exception:  # сеть/Telegram - попробуем в следующий раз, бот не роняем
                log.exception("auto backup failed")
            await asyncio.sleep(self.every)

    async def shutdown(self) -> None:
        """Остановка (обычно деплой): последняя попытка сохранить свежие данные."""
        import asyncio
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if not self.every:
            return
        try:
            await asyncio.wait_for(self.run_once("Копия перед остановкой: "), timeout=20)
        except Exception:
            pass
