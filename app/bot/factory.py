from __future__ import annotations

# # import asyncpg  # removed for SQLite  # removed for SQLite
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

from app.bot.context import Services
from app.config import Config
from app.handlers import (
    admin_broadcast,
    admin_deputies,
    admin_history,
    admin_notify,
    admin_panel,
    fallback,
    registration,
    start,
    team,
)
from app.middlewares.access import AccessMiddleware
from app.middlewares.core import UserTrackMiddleware
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


def build_bot(cfg: Config) -> Bot:
    return Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


def build_services(cfg: Config, pool: object, bot: Bot) -> Services:
    return Services(
        cfg=cfg,
        reg=RegistrationService(pool),
        admin=AdminService(pool),
        sub=SubscriptionService(bot, cfg.channel_id, cfg.chat_id),
        bc=BroadcastService(pool, bot),
        team=TeamService(pool),
        bot=bot,
        series=SeriesService(pool, bot),
        tournaments=TournamentService(pool),
        deputies=DeputyService(pool),
        result_notify=ResultNotifyService(pool, bot),
        roles=RoleService(pool, cfg),
    )


def build_dispatcher(svc: Services, pool: object) -> Dispatcher:
    # SimpleEventIsolation serialises updates per (chat, user) *before* the FSM state is read, so a
    # double-tap on "confirm" can never run two handlers against the same state.
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation(), svc=svc)
    # registered after aiogram's own UserContext/FSM middlewares, so event_from_user is available
    # The access gate goes first: nothing below it (DB tracking, filters, handlers) runs for a denied user.
    dp.update.outer_middleware(AccessMiddleware(svc.cfg))
    dp.update.outer_middleware(UserTrackMiddleware(pool))
    # order matters: deep-link /start (team) before plain /start & menu, admin routers gate themselves
    # (IsStaff, or IsAdmin for deputy management), fallback last
    for r in (
        team.router, start.router, admin_panel.router, admin_notify.router,
        admin_deputies.router, admin_history.router, admin_broadcast.router, registration.router, fallback.router,
    ):
        dp.include_router(r)
    return dp
