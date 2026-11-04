"""The individual games ("катки") inside a best-of-5 series (a `matches` row). The series' score and
winner are always derived from the recorded games - never entered directly. A series completing
triggers a best-effort default notification to every real team_member of both sides. Series (match)
CRUD itself (create/list/delete/bracket) stays in AdminService, unchanged."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.repositories import games as games_repo
from app.repositories import matches as matches_repo
from app.repositories import teams as teams_repo
from app.repositories import users as users_repo
from app.services.errors import InvalidGameWinner, NotFound, ResultLocked, SeriesDecided
from app.services.registration import TgUser
from app.utils.text import esc

log = logging.getLogger(__name__)

_SERIES_DECIDED, _INVALID_WINNER = "MB006", "MB007"
BEST_OF = 5


@dataclass(frozen=True)
class SeriesView:
    match: dict
    games: tuple[dict, ...]
    wins_a: int
    wins_b: int

    @property
    def next_game_number(self) -> int:
        return min(len(self.games) + 1, BEST_OF)


class SeriesService:
    def __init__(self, pool: object, bot: Bot):
        self.pool, self.bot = pool, bot

    async def game_and_series(self, game_id: int) -> tuple[dict, SeriesView] | tuple[None, None]:
        async with self.pool.acquire() as conn:
            g = await games_repo.get_game(conn, game_id)
        if not g:
            return None, None
        return g, await self.series_view(g["match_id"])

    async def series_view(self, match_id: int) -> SeriesView:
        async with self.pool.acquire() as conn:
            m = await matches_repo.get(conn, match_id)
            if not m:
                raise NotFound("Матч")
            gs = tuple(await games_repo.games(conn, match_id))
        wins_a = sum(1 for g in gs if g["winner_team_id"] == m["team_a_id"])
        wins_b = sum(1 for g in gs if g["winner_team_id"] == m["team_b_id"])
        return SeriesView(m, gs, wins_a, wins_b)

    async def add_game(
        self, actor: TgUser, match_id: int, winner_team_id: int, expected_game_number: Optional[int] = None
    ) -> SeriesView:
        """`expected_game_number`, when given, makes a repeated tap of the same "confirm" button a
        no-op instead of silently recording a second game: if the series has already moved past that
        slot (someone else's click, or this same click already landing once), nothing is inserted and
        the current state is returned as-is."""
        stale = False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await users_repo.upsert_user(conn, actor.id, actor.username, actor.full_name)
                before = await matches_repo.get(conn, match_id)
                if not before:
                    raise NotFound("Матч")
                if winner_team_id not in (before["team_a_id"], before["team_b_id"]):
                    raise InvalidGameWinner()
                await games_repo.lock_match(conn, match_id)  # serialises this check with the insert below
                if expected_game_number is not None and expected_game_number != await games_repo.game_count(conn, match_id) + 1:
                    stale = True
                else:
                    try:
                        await games_repo.add_game(conn, match_id, winner_team_id, actor.id)
                    except asyncpg.PostgresError as e:
                        self._map_game_error(e)
                        raise
            view = await self.series_view(match_id)
        if not stale and before["status"] != "finished" and view.match["status"] == "finished":
            await self._notify_series_result(view)
        return view

    async def edit_game(self, actor: TgUser, game_id: int, winner_team_id: int) -> SeriesView:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await users_repo.upsert_user(conn, actor.id, actor.username, actor.full_name)
                g = await games_repo.get_game(conn, game_id)
                if not g:
                    raise NotFound("Катка")
                m = await matches_repo.get(conn, g["match_id"])
                if not m:
                    raise NotFound("Матч")  # pragma: no cover - defensive, FK guarantees this
                if winner_team_id not in (m["team_a_id"], m["team_b_id"]):
                    raise InvalidGameWinner()
                if m["status"] == "finished":
                    other = await games_repo.other_matches_involving(
                        conn, m["tournament_id"], m["id"], m["team_a_id"], m["team_b_id"]
                    )
                    if other:
                        raise ResultLocked()
                try:
                    if not await games_repo.edit_game(conn, game_id, winner_team_id, actor.id):
                        raise NotFound("Катка")
                except asyncpg.PostgresError as e:
                    self._map_game_error(e)
                    raise
            was_finished = m["status"] == "finished"
            view = await self.series_view(m["id"])
        if not was_finished and view.match["status"] == "finished":
            await self._notify_series_result(view)
        return view

    @staticmethod
    def _map_game_error(e: asyncpg.PostgresError) -> None:
        if e.sqlstate == _SERIES_DECIDED:
            raise SeriesDecided()
        if e.sqlstate == _INVALID_WINNER:
            raise InvalidGameWinner()

    # ---- default notification on series completion --------------------------------------------
    def _template(self, view: SeriesView, for_winner: bool) -> str:
        m, wa, wb = view.match, view.wins_a, view.wins_b
        stage = m["stage_code"]
        my_team = m["winner"] if for_winner else (m["team_b"] if m["winner_id"] == m["team_a_id"] else m["team_a"])
        opp_team = (m["team_b"] if m["winner_id"] == m["team_a_id"] else m["team_a"]) if for_winner else m["winner"]
        my_score, opp_score = (max(wa, wb), min(wa, wb)) if for_winner else (min(wa, wb), max(wa, wb))
        head = f"🏆 <b>ПОБЕДА!</b>\n\nВаша команда «{esc(my_team)}» выиграла матч!" if for_winner else \
               f"🎮 <b>МАТЧ ЗАВЕРШЁН</b>\n\nВаша команда «{esc(my_team)}» завершила матч."
        body = f"\n\n⚔️ Соперник: {esc(opp_team)}\n📊 Итоговый счёт: {my_score}:{opp_score}\n🏆 Этап: {esc(m['stage_title'])}"
        if stage == "final":
            if for_winner:
                return (f"🏆 <b>ВЫ — ЧЕМПИОНЫ!</b>\n\nКоманда «{esc(my_team)}» выиграла турнир!\n\n"
                        f"👑 1 место\n📊 Финальный счёт: {my_score}:{opp_score}\n\nПоздравляем! 🏆")
            return (f"🏆 <b>ТУРНИРНЫЙ ФИНАЛ ЗАВЕРШЁН</b>\n\nКоманда «{esc(my_team)}»\n\n"
                    f"🥈 2 место\n\n📊 Финальный счёт: {my_score}:{opp_score}\n\nСпасибо за отличную игру! ❤️")
        if stage == "third":
            if for_winner:
                return (f"🥉 <b>3 МЕСТО!</b>\n\nПоздравляем команду «{esc(my_team)}»!\n\nВы выиграли матч за третье место.\n\n"
                        f"📊 Итоговый счёт: {my_score}:{opp_score}\n\n🥉 Вы занимаете 3 место в турнире!")
            return head + body + "\n\nСпасибо за игру! ❤️"
        if stage == "semi":
            return head + body + ("\n\nВы проходите в финал! 🔥" if for_winner else "\n\nВы сыграете матч за 3 место.")
        return head + body + ("\n\nВы проходите дальше! 🔥" if for_winner else "\n\nСпасибо за игру! ❤️")

    async def _notify_series_result(self, view: SeriesView) -> None:
        m = view.match
        loser_id = m["team_b_id"] if m["winner_id"] == m["team_a_id"] else m["team_a_id"]
        async with self.pool.acquire() as conn:
            winners = await teams_repo.members(conn, m["winner_id"])
            losers = await teams_repo.members(conn, loser_id)
        win_text, lose_text = self._template(view, True), self._template(view, False)
        for members, text in ((winners, win_text), (losers, lose_text)):
            for mem in members:
                try:
                    await self.bot.send_message(mem["user_id"], text)
                except TelegramAPIError:
                    log.info("series result notification to %s not delivered", mem["user_id"])
