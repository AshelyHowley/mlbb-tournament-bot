"""Drives the real Dispatcher/handlers/FSM with a fake Telegram transport (no network)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChatMember,
    GetMe,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    Document,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
    Video,
)

from app.bot.factory import build_dispatcher, build_services
from app.config import Config
from app.handlers import (
    admin_broadcast,
    admin_deputies,
    admin_history,
    admin_matches,
    admin_notify,
    admin_panel,
    fallback,
    registration,
    start,
    team,
)

TOKEN = "123456789:" + "A" * 35
ADMIN_ID = 999


class _ErrCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errors: list[logging.LogRecord] = []

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            self.errors.append(record)


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests: list = []
        self.chat_state: dict = {}  # chat_id -> "member" | "left" | Exception
        self.fail_send: dict = {}  # chat_id -> Exception to raise instead of "sending" (retry-flow tests)
        self._n = 1000

    async def close(self):  # pragma: no cover
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        self._n += 1
        if isinstance(method, (SendMessage, EditMessageText, SendPhoto, SendVideo, SendDocument)):
            cid = method.chat_id if isinstance(method.chat_id, int) else 1
            if cid in self.fail_send:
                raise self.fail_send[cid]
            return Message(message_id=self._n, date=datetime.datetime('now'), chat=Chat(id=cid, type="private"),
                           text=getattr(method, "text", None), caption=getattr(method, "caption", None))
        if isinstance(method, GetChatMember):
            st = self.chat_state.get(method.chat_id, "member")
            if isinstance(st, Exception):
                raise st
            user = User(id=method.user_id, is_bot=False, first_name="x")
            return ChatMemberMember(status="member", user=user) if st == "member" else ChatMemberLeft(status="left", user=user)
        if isinstance(method, GetMe):
            return User(id=4242, is_bot=True, first_name="T", username="Test_Tournament_bot")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True


class Harness:
    def __init__(self, pool, admin_id: int = ADMIN_ID, access_mode: str = "public", allowed=()):
        self.cfg = Config(
            bot_token=TOKEN, admin_id=admin_id, database_url="postgresql://u:p@h/db",
            channel_id="@yuliataetae", channel_url="https://t.me/yuliataetae",
            chat_id="@MLBangBangTournamentChat", chat_url="https://t.me/MLBangBangTournamentChat",
            access_mode=access_mode, allowed_user_ids=frozenset(allowed),
        )
        self.tg = FakeTelegram()
        self.bot = Bot(TOKEN, session=self.tg)
        self.svc = build_services(self.cfg, pool, self.bot)
        self.svc.bc.interval = 0
        for r in (start, team, admin_panel, admin_matches, admin_notify, admin_deputies, admin_history, admin_broadcast, registration, fallback):
            r.router._parent_router = None  # module-level routers are reused across per-test dispatchers
        self.dp = build_dispatcher(self.svc, pool)
        self._uid = 0
        self.capture = _ErrCapture()
        logging.getLogger("app.handlers.fallback").addHandler(self.capture)

    def close(self):
        logging.getLogger("app.handlers.fallback").removeHandler(self.capture)

    # -- inputs
    def _user(self, uid: int, username: Optional[str]) -> User:
        return User(id=uid, is_bot=False, first_name=f"U{uid}", username=username)

    def _msg(self, uid, username, text=None, chat_type="private", **kw) -> Message:
        self._uid += 1
        ents = None
        if text and text.startswith("/"):
            ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        return Message(message_id=self._uid, date=datetime.datetime('now'), chat=Chat(id=uid, type=chat_type),
                       from_user=self._user(uid, username), text=text, entities=ents, **kw)

    async def say(self, uid: int, username: Optional[str], text: str, chat_type: str = "private") -> None:
        self._uid += 1
        await self.dp.feed_update(self.bot, Update(update_id=self._uid, message=self._msg(uid, username, text, chat_type)))

    async def media(self, uid: int, username: Optional[str], kind: str, file_id: str, caption: Optional[str] = None) -> None:
        """Feeds a synthetic photo/video/document Message (no real file upload/network involved)."""
        self._uid += 1
        extra = {"caption": caption}
        if kind == "photo":
            extra["photo"] = [PhotoSize(file_id=file_id, file_unique_id=file_id + "u", width=90, height=90)]
        elif kind == "video":
            extra["video"] = Video(file_id=file_id, file_unique_id=file_id + "u", width=640, height=480, duration=5)
        elif kind == "document":
            extra["document"] = Document(file_id=file_id, file_unique_id=file_id + "u")
        else:
            raise ValueError(kind)
        await self.dp.feed_update(self.bot, Update(update_id=self._uid, message=self._msg(uid, username, None, "private", **extra)))

    async def press(self, uid: int, username: Optional[str], data: str) -> None:
        self._uid += 1
        cq = CallbackQuery(id=str(self._uid), from_user=self._user(uid, username), chat_instance="ci", data=data,
                           message=self._msg(uid, username, "old"))
        await self.dp.feed_update(self.bot, Update(update_id=self._uid, callback_query=cq))

    # -- outputs
    def clear(self):
        self.tg.requests.clear()

    def texts(self) -> list[str]:
        return [t for m in self.tg.requests
                if isinstance(m, (SendMessage, EditMessageText, SendPhoto, SendVideo, SendDocument))
                for t in [getattr(m, "text", None) or getattr(m, "caption", None)] if t]

    def last(self) -> str:
        return self.texts()[-1] if self.texts() else ""

    def buttons(self) -> list[str]:
        out = []
        for m in self.tg.requests:
            rm = getattr(m, "reply_markup", None)
            if rm:
                out += [b.text for row in rm.inline_keyboard for b in row]
        return out

    def answers(self) -> list[AnswerCallbackQuery]:
        return [m for m in self.tg.requests if isinstance(m, AnswerCallbackQuery)]

    @property
    def errors(self):
        return self.capture.errors
