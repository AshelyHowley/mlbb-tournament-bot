"""Картинка сетки активного турнира - общая для админ-панели и публичного «🏆 Турнирная сетка».

Рисование (Pillow) идёт в отдельном потоке, а результат кэшируется по содержимому сетки:
если её одновременно открывают много участников, картинка рисуется один раз, пока сетка не изменится.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.services.bracket_image import render_bracket_image

BRACKET_ROWS_SQL = (
    "SELECT m.id, m.stage_code AS stage, m.next_match_id, "
    "       ta.name AS team_a, tb.name AS team_b, tw.name AS winner "
    "  FROM matches m JOIN stages s ON s.code = m.stage_code "
    "  LEFT JOIN teams ta ON ta.id = m.team_a_id "
    "  LEFT JOIN teams tb ON tb.id = m.team_b_id "
    "  LEFT JOIN teams tw ON tw.id = m.winner_id "
    " WHERE m.tournament_id = $1 ORDER BY s.sort_order, m.id"
)

_cache: dict[str, object] = {"key": None, "png": None}


async def active_bracket_png(pool, title: str = "Турнир") -> Optional[bytes]:
    """PNG сетки активного турнира или None, если в сетке ещё нет ни одного матча."""
    async with pool.acquire() as conn:
        tid = await conn.fetchval("SELECT id FROM tournaments WHERE status = 'active' LIMIT 1")
        rows = [dict(r) for r in await conn.fetch(BRACKET_ROWS_SQL, tid)] if tid else []
    if not any(r["stage"] != "finalist" for r in rows):
        return None
    key = repr((title, [tuple(r.values()) for r in rows]))
    if _cache["key"] == key:
        return _cache["png"]  # type: ignore[return-value]
    png = await asyncio.to_thread(render_bracket_image, rows, title)
    _cache["key"], _cache["png"] = key, png
    return png
