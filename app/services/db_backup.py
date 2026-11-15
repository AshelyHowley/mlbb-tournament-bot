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
                      silent: bool = False, pin: bool = True) -> list[tuple[str, bytes]]:
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
        msg = await bot.send_document(
            chat_id, BufferedInputFile(data, filename=f"{name}_{stamp}.db"),
            caption=f"💾 {label}{name}.db · {stamp}", disable_notification=silent,
        )
        sent.append((name, data))
        # пустую базу не закрепляем никогда: иначе после «сервер всё стёр» она вытеснит хорошую копию
        if pin and name == "tournament" and await asyncio.to_thread(_has_data, data):
            await _pin(bot, chat_id, msg.message_id)
    return sent


async def _pin(bot, chat_id: int, message_id: int) -> None:
    """Последняя копия базы - закреплённое сообщение в чате с админом: оттуда её достаёт
    «📌 Из последней автокопии», даже если сервер стёр всё. Старые закрепы снимаем,
    чтобы закреплённой всегда была самая свежая копия."""
    try:
        await bot.unpin_all_chat_messages(chat_id)
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
    except Exception:  # закрепить не вышло - сама копия всё равно отправлена
        import logging
        logging.getLogger(__name__).warning("could not pin the backup message", exc_info=True)


async def find_pinned_backup(bot, chat_id: int):
    """Закреплённое сообщение с копией tournament_*.db, присланной этим ботом, или None."""
    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        return None
    m = getattr(chat, "pinned_message", None)
    doc = getattr(m, "document", None) if m else None
    if not doc or not (doc.file_name or "").startswith("tournament_"):
        return None
    me = await bot.me()
    if m.from_user is not None and m.from_user.id != me.id:
        return None  # закрепил кто-то другой - не наша копия
    return m


def has_data(path: str) -> bool:
    """Есть ли в базе по пути `path` хоть что-то (для проверки «сервер стёр базу»)."""
    try:
        return _has_data(snapshot(path))
    except (FileNotFoundError, sqlite3.DatabaseError):
        return False


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


# ---------------------------------------------------------------------------------------- restore
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # бот может скачать из Telegram файл не больше 20 МБ


class BadBackup(Exception):
    """Файл не похож на копию базы бота. Текст - для пользователя."""


def inspect_backup(path: str) -> dict:
    """Проверяет присланный файл. Возвращает {'kind': 'tournament'|'player_archive', ...счётчики}."""
    try:
        c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        raise BadBackup("Это не файл базы данных.")
    try:
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BadBackup("Файл базы повреждён.")
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError:
            raise BadBackup("Это не файл базы данных.")
        if {"teams", "solo_players", "tournaments"} <= tables:
            count = lambda sql: c.execute(sql).fetchone()[0]
            return {
                "kind": "tournament",
                "teams": count("SELECT count(*) FROM teams t JOIN tournaments r ON r.id = t.tournament_id "
                               "WHERE r.status = 'active'"),
                "solo": count("SELECT count(*) FROM solo_players s JOIN tournaments r ON r.id = s.tournament_id "
                              "WHERE r.status = 'active'"),
                "history": count("SELECT count(*) FROM tournaments WHERE status <> 'active'"),
            }
        if "players" in tables:
            return {"kind": "player_archive", "players": c.execute("SELECT count(*) FROM players").fetchone()[0]}
        raise BadBackup("Это база, но не от этого бота.")
    finally:
        c.close()


def restore_into(src_path: str, dst_path: str) -> None:
    """Заменяет содержимое базы dst_path содержимым src_path (backup API: атомарно для читателей,
    живые соединения бота сразу видят новые данные - перезапуск не нужен)."""
    src = sqlite3.connect(f"file:{Path(src_path).as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def offer_restore_if_empty(bot, cfg) -> bool:
    """При запуске: база пустая (сервер её стёр), а в чате с админом закреплена копия -
    предлагаем восстановить одной кнопкой. Сами ничего не заменяем: решает админ."""
    import asyncio

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    path = sqlite_path(cfg.database_url)
    if not path or await asyncio.to_thread(has_data, path):
        return False
    pinned = await find_pinned_backup(bot, cfg.admin_id)
    if pinned is None:
        return False
    await bot.send_message(
        cfg.admin_id,
        "⚠️ <b>Бот запустился с пустой базой</b>\n\n"
        "Похоже, сервер стёр данные (например, при деплое). В чате закреплена последняя копия: "
        f"<b>{pinned.document.file_name}</b>.\n\nВосстановить из неё?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📌 Восстановить из копии", callback_data="adm:restore:pinned")],
        ]),
    )
    return True
