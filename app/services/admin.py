"""Admin-side operations: teams, solo players, matches. Authorisation is enforced at the handler layer
(AdminFilter on the router); these methods contain no Telegram code."""
from __future__ import annotations
import sqlite3

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.repositories import matches as matches_repo
from app.repositories import solo as solo_repo
from app.repositories import teams as teams_repo
from app.services.errors import Conflict, NotFound, TeamNotEligible, ValidationError

PAGE_SIZE = 8


class AdminService:
    def __init__(self, pool: object):
        self.pool = pool

    # ---- teams -----------------------------------------------------------
    async def teams_page(self, page: int):
        page = max(page, 0)
        async with self.pool.acquire() as conn:
            total = await teams_repo.count(conn)
            last = max((total - 1) // PAGE_SIZE, 0)
            page = min(page, last)
            rows = await teams_repo.list_page(conn, PAGE_SIZE, page * PAGE_SIZE)
        return total, page, last, rows

    async def team_card(self, team_id: int):
        async with self.pool.acquire() as conn:
            team = await teams_repo.get(conn, team_id)
            if not team:
                raise NotFound("Команда")
            return team, await teams_repo.members(conn, team_id)

    async def delete_team(self, team_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Clear next_match_id references first
                await conn.execute(
                    "UPDATE matches SET next_match_id = NULL WHERE next_match_id IN "
                    "(SELECT id FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1)",
                    team_id,
                )
                await conn.execute("DELETE FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1", team_id)
                await conn.execute("DELETE FROM team_members WHERE team_id = $1", team_id)
                await conn.execute("DELETE FROM teams WHERE id = $1", team_id)

    async def all_teams(self):
        async with self.pool.acquire() as conn:
            return await teams_repo.list_all(conn)

    # ---- solo ------------------------------------------------------------
    async def solo_page(self, page: int):
        page = max(page, 0)
        async with self.pool.acquire() as conn:
            total = await solo_repo.count(conn)
            last = max((total - 1) // PAGE_SIZE, 0)
            page = min(page, last)
            rows = await solo_repo.list_page(conn, PAGE_SIZE, page * PAGE_SIZE)
        return total, page, last, rows

    async def solo_card(self, user_id: int):
        async with self.pool.acquire() as conn:
            row = await solo_repo.get(conn, user_id)
        if not row:
            raise NotFound("Игрок")
        return row

    async def delete_solo(self, user_id: int) -> None:
        async with self.pool.acquire() as conn:
            if not await solo_repo.delete(conn, user_id):
                raise NotFound("Игрок")

    # ---- matches ---------------------------------------------------------
    async def stages(self):
        async with self.pool.acquire() as conn:
            return await matches_repo.stages(conn)

    async def create_match(self, team_a: int, team_b: int, stage: str) -> int:
        if team_a == team_b:
            raise ValidationError("❌ Команда не может играть сама с собой.")
        try:
            async with self.pool.acquire() as conn:
                if not await matches_repo.stage_exists(conn, stage):
                    raise ValidationError("❌ Неизвестный этап.")
                return await matches_repo.create(conn, stage, team_a, team_b)
        except sqlite3.IntegrityError as e:
            if str(e) == "matches_one_final_uq":
                raise Conflict("ℹ️ Финал этого турнира уже создан.")
            if str(e) == "matches_one_third_uq":
                raise Conflict("ℹ️ Матч за 3-е место этого турнира уже создан.")
            raise Conflict("ℹ️ Такой матч на этом этапе уже создан.")
        except Exception:
            raise NotFound("Команда")
        except Exception:
            raise ValidationError("❌ Некорректные участники матча.")
        except sqlite3.IntegrityError as e:
            if e.sqlstate == "MB005":
                raise TeamNotEligible()
            if e.sqlstate == "MB012":
                raise NotFound("Команда")
            raise

    async def match(self, match_id: int):
        async with self.pool.acquire() as conn:
            m = await matches_repo.get(conn, match_id)
        if not m:
            raise NotFound("Матч")
        return m

    async def matches_page(self, status: str, page: int):
        page = max(page, 0)
        async with self.pool.acquire() as conn:
            total = await matches_repo.count_by_status(conn, status)
            last = max((total - 1) // PAGE_SIZE, 0)
            page = min(page, last)
            rows = await matches_repo.list_by_status(conn, status, PAGE_SIZE, page * PAGE_SIZE)
        return total, page, last, rows

    async def delete_match(self, match_id: int) -> None:
        async with self.pool.acquire() as conn:
            if not await matches_repo.delete(conn, match_id):
                raise NotFound("Матч")

    async def bracket_rows(self):
        async with self.pool.acquire() as conn:
            return await matches_repo.list_all(conn)
