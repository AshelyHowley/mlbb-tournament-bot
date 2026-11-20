"""Staff audits of everyone registered in the active tournament: missing Telegram @username, and
the channel/chat subscription condition. Each audit can message the offenders through the bot."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from app.repositories import users as users_repo
from app.services.subscription import SubscriptionService
from app.utils.text import person_plain

log = logging.getLogger(__name__)

FAKE_UID_BASE = 9_000_000_000  # test teams' fake captains: never real Telegram ids
CALL_INTERVAL = 0.05  # keeps getChat/getChatMember well under Telegram's rate limits

USERNAME_NOTICE = (
    "⚠️ <b>Нужен username в Telegram</b>\n\n"
    "Вы зарегистрированы на турнир, но у вашего аккаунта нет username (@имя). "
    "Без него организаторы и соперники не смогут с вами связаться.\n\n"
    "Откройте Telegram → Настройки → Имя пользователя, задайте его и нажмите /start в боте."
)


def sub_notice(channel_ok: bool, chat_ok: bool) -> str:
    lines = ["⚠️ <b>Условия участия в турнире</b>\n", "Все участники должны состоять в канале и чате турнира:\n"]
    lines.append(("✅" if channel_ok else "❌") + " Канал")
    lines.append(("✅" if chat_ok else "❌") + " Чат")
    lines.append("\nПодпишитесь, иначе регистрация может быть аннулирована.")
    return "\n".join(lines)


@dataclass(frozen=True)
class Participant:
    user_id: int
    username: Optional[str]
    full_name: str
    where: str  # team name or "соло"
    is_captain: bool = False

    @property
    def label(self) -> str:
        return f"{person_plain(self.full_name, self.username, self.user_id)} — {self.where}"


@dataclass
class Offender:
    p: Participant
    channel_ok: bool = True
    chat_ok: bool = True


@dataclass
class AuditReport:
    total: int = 0
    offenders: list[Offender] = field(default_factory=list)
    errors: list[Participant] = field(default_factory=list)  # Telegram could not answer about them
    notified: int = 0
    not_delivered: int = 0


async def participants(pool) -> list[Participant]:
    """Everyone who plays in the active tournament (team members incl. captains, and solo players)."""
    active = "(SELECT id FROM tournaments WHERE status = 'active')"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT m.user_id AS uid, u.username, u.full_name, t.name AS place, m.is_captain AS cap
              FROM team_members m JOIN teams t ON t.id = m.team_id JOIN users u ON u.telegram_id = m.user_id
             WHERE t.tournament_id = {active}
            UNION ALL
            SELECT s.user_id, u.username, u.full_name, 'соло', 0
              FROM solo_players s JOIN users u ON u.telegram_id = s.user_id
             WHERE s.tournament_id = {active}
            ORDER BY 4, 3
            """
        )
    seen, out = set(), []
    for r in rows:
        if r["uid"] in seen or r["uid"] >= FAKE_UID_BASE:
            continue
        seen.add(r["uid"])
        out.append(Participant(r["uid"], r["username"], r["full_name"] or "", r["place"], bool(r["cap"])))
    return out


async def _notify(bot: Bot, uid: int, text: str, markup=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=markup)
        return True
    except TelegramForbiddenError:
        return False
    except TelegramAPIError:
        log.info("audit notice to %s not delivered", uid)
        return False


class ParticipantCheckService:
    def __init__(self, pool, bot: Bot, sub: SubscriptionService, *, interval: float = CALL_INTERVAL):
        self.pool, self.bot, self.sub, self.interval = pool, bot, sub, interval

    async def _fresh_username(self, p: Participant) -> Optional[str]:
        """The stored username may be stale (set after the user last wrote to the bot): ask Telegram."""
        try:
            chat = await asyncio.wait_for(self.bot.get_chat(p.user_id), 10.0)
        except (TelegramAPIError, asyncio.TimeoutError, OSError):
            return p.username
        uname = getattr(chat, "username", None)
        if uname != p.username:
            async with self.pool.acquire() as conn:
                await users_repo.upsert_user(conn, p.user_id, uname, p.full_name)
        return uname

    async def usernames(self, notify: bool = True) -> AuditReport:
        people = await participants(self.pool)
        rep = AuditReport(total=len(people))
        for p in people:
            if p.username:
                continue
            uname = await self._fresh_username(p)
            if self.interval:
                await asyncio.sleep(self.interval)
            if uname:
                continue
            rep.offenders.append(Offender(p))
            if notify:
                if await _notify(self.bot, p.user_id, USERNAME_NOTICE):
                    rep.notified += 1
                else:
                    rep.not_delivered += 1
        return rep

    async def subscriptions(self, notify: bool = False, markup=None) -> AuditReport:
        people = await participants(self.pool)
        rep = AuditReport(total=len(people))
        for p in people:
            res = await self.sub.check(p.user_id)
            if self.interval:
                await asyncio.sleep(self.interval)
            if res.error:
                rep.errors.append(p)
                continue
            if res.ok:
                continue
            rep.offenders.append(Offender(p, res.channel_ok, res.chat_ok))
            if notify:
                if await _notify(self.bot, p.user_id, sub_notice(res.channel_ok, res.chat_ok), markup):
                    rep.notified += 1
                else:
                    rep.not_delivered += 1
        return rep
