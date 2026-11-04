"""Tournament lifecycle: the single ACTIVE tournament, its readiness for completion, and the
read-only archive/history left behind once it is finalised. Nothing here ever deletes data -
"finishing" a tournament means archiving it and opening a fresh, empty one."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.repositories import games as games_repo
from app.repositories import matches as matches_repo
from app.repositories import teams as teams_repo
from app.repositories import tournaments as tournaments_repo
from app.repositories import users as users_repo
from app.services.errors import NotFound, TournamentNotReady
from app.services.registration import TgUser

CONFIRM_WORD = "ЗАВЕРШИТЬ"


@dataclass(frozen=True)
class Placements:
    first_id: Optional[int]
    first_name: Optional[str]
    second_id: Optional[int]
    second_name: Optional[str]
    third_id: Optional[int]
    third_name: Optional[str]

    @property
    def ready(self) -> bool:
        return self.first_id is not None and self.second_id is not None and self.third_id is not None


@dataclass(frozen=True)
class TournamentSummary:
    id: int
    status: str
    started_at: datetime
    completed_at: Optional[datetime]


class TournamentService:
    def __init__(self, pool: object):
        self.pool = pool

    async def active_id(self) -> int:
        async with self.pool.acquire() as conn:
            return await tournaments_repo.active_id(conn)

    async def _placements(self, conn, tournament_id: int) -> Placements:
        final = await matches_repo.get_by_stage(conn, tournament_id, "final")
        third = await matches_repo.get_by_stage(conn, tournament_id, "third")
        first_id = first_name = second_id = second_name = None
        if final and final["status"] == "finished":
            first_id, first_name = final["winner_id"], final["winner"]
            second_id = final["team_b_id"] if final["winner_id"] == final["team_a_id"] else final["team_a_id"]
            second_name = final["team_b"] if final["winner_id"] == final["team_a_id"] else final["team_a"]
        third_id = third_name = None
        if third and third["status"] == "finished":
            third_id, third_name = third["winner_id"], third["winner"]
        return Placements(first_id, first_name, second_id, second_name, third_id, third_name)

    async def placements(self) -> Placements:
        async with self.pool.acquire() as conn:
            tid = await tournaments_repo.active_id(conn)
            return await self._placements(conn, tid)

    # ---- finalize / archive -------------------------------------------------------------------
    async def finalize(self, actor: TgUser, *, force: bool) -> tuple[int, Placements]:
        """Atomically: snapshot placements onto the active tournament, mark it completed (or
        closed_early), and open a brand-new empty active tournament. `force` must be explicit
        confirmation for an early close (missing final and/or third-place result)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await users_repo.upsert_user(conn, actor.id, actor.username, actor.full_name)
                row = await tournaments_repo.get_active(conn, lock=True)
                if not row:
                    raise NotFound("Активный турнир")  # pragma: no cover - invariant, should never happen
                placements = await self._placements(conn, row["id"])
                if not placements.ready and not force:
                    raise TournamentNotReady(
                        "⚠️ <b>Турнир ещё не завершён</b>\n\nНе все призовые места определены."
                    )
                status = "completed" if placements.ready else "closed_early"
                await tournaments_repo.snapshot_display(conn, row["id"])  # freeze names/usernames now
                await tournaments_repo.finalize(
                    conn, row["id"], status, actor.id, placements.first_id, placements.second_id, placements.third_id
                )
                await tournaments_repo.create_new_active(conn)
            return row["id"], placements

    # ---- history --------------------------------------------------------------------------------
    async def history_page(self, page: int, page_size: int = 8):
        page = max(page, 0)
        async with self.pool.acquire() as conn:
            total = await tournaments_repo.history_count(conn)
            last = max((total - 1) // page_size, 0)
            page = min(page, last)
            rows = await tournaments_repo.history_page(conn, page_size, page * page_size)
        return total, page, last, rows

    async def get(self, tournament_id: int):
        async with self.pool.acquire() as conn:
            row = await tournaments_repo.get(conn, tournament_id)
        if not row:
            raise NotFound("Турнир")
        return row

    async def detail(self, tournament_id: int):
        async with self.pool.acquire() as conn:
            row = await tournaments_repo.get(conn, tournament_id)
            if not row:
                raise NotFound("Турнир")
            stats = await tournaments_repo.stats(conn, tournament_id)
        return row, stats

    async def teams(self, tournament_id: int):
        async with self.pool.acquire() as conn:
            return await tournaments_repo.teams_of(conn, tournament_id)

    async def solo(self, tournament_id: int):
        async with self.pool.acquire() as conn:
            return await tournaments_repo.solo_of(conn, tournament_id)

    async def bracket(self, tournament_id: int):
        async with self.pool.acquire() as conn:
            return await matches_repo.list_for_tournament(conn, tournament_id)

    async def series_games(self, match_id: int):
        async with self.pool.acquire() as conn:
            return await games_repo.games(conn, match_id)

    async def bo5_breakdown(self, tournament_id: int):
        async with self.pool.acquire() as conn:
            return await tournaments_repo.bo5_breakdown(conn, tournament_id)

    async def team_card(self, team_id: int):
        async with self.pool.acquire() as conn:
            team = await teams_repo.get(conn, team_id)
            if not team:
                raise NotFound("Команда")
            members = await teams_repo.members(conn, team_id)
            played = await conn.fetch(
                """
                SELECT m.id, m.stage_code, s.title AS stage_title, m.status, m.winner_id, m.team_a_id, m.team_b_id,
                       ta.name AS team_a, tb.name AS team_b
                  FROM matches m JOIN stages s ON s.code = m.stage_code
                  JOIN teams ta ON ta.id = m.team_a_id JOIN teams tb ON tb.id = m.team_b_id
                 WHERE m.team_a_id = $1 OR m.team_b_id = $1
                 ORDER BY s.sort_order, m.id
                """,
                team_id,
            )
        return team, members, played
