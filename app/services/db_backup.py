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
