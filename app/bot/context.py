from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from app.config import Config
from app.services.admin import AdminService
from app.services.broadcast import BroadcastService
from app.services.deputy import DeputyService
from app.services.registration import RegistrationService
from app.services.result_notify import ResultNotifyService
from app.services.roles import RoleService
from app.services.series import SeriesService
from app.services.subscription import SubscriptionService
from app.services.team import TeamService
from app.services.tournament import TournamentService


@dataclass
class Services:
    cfg: Config
    reg: RegistrationService
    admin: AdminService
    sub: SubscriptionService
    bc: BroadcastService
    team: TeamService
    bot: Bot
    series: SeriesService
    tournaments: TournamentService
    deputies: DeputyService
    result_notify: ResultNotifyService
    roles: RoleService

    async def invite_link(self, token: str) -> str:
        me = await self.bot.me()  # cached by aiogram after the first call
        return f"https://t.me/{me.username}?start=team_{token}"

    async def captain_link(self, token: str) -> str:
        me = await self.bot.me()
        return f"https://t.me/{me.username}?start=captain_{token}"
