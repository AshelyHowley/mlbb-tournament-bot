"""«Список команд» для админа: все команды активного турнира с названием и звёздами.

Звёзды команды - сумма рангов её игроков (как «Суммарный ранг» в карточке команды)."""
from __future__ import annotations

from dataclasses import dataclass

from app.utils.text import esc

_SQL = """
    SELECT t.name, COALESCE(SUM(m.rank), 0) AS stars
      FROM teams t LEFT JOIN team_members m ON m.team_id = t.id
     WHERE t.tournament_id = (SELECT id FROM tournaments WHERE status = 'active')
     GROUP BY t.id
     ORDER BY t.created_at, t.id
"""


@dataclass(frozen=True)
class TeamRow:
    name: str
    stars: int


async def load(pool) -> list[TeamRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SQL)
    return [TeamRow(r["name"], int(r["stars"] or 0)) for r in rows]


def _blocks(teams: list[TeamRow]) -> list[str]:
    """По убыванию звёзд, медали тройке лидеров."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    ranked = sorted(teams, key=lambda t: -t.stars)
    head = "🏆 <b>УЧАСТНИКИ ТУРНИРА</b> 🏆\n━━━━━━━━━━━━━━━━━━"
    lines = [f"{medals.get(i, f'<b>{i}.</b>')} <b>{esc(t.name)}</b> — ⭐ {t.stars}" for i, t in enumerate(ranked, 1)]
    total = sum(t.stars for t in teams)
    foot = f"━━━━━━━━━━━━━━━━━━\n👥 Команд: <b>{len(teams)}</b>  ·  ⭐ Всего звёзд: <b>{total}</b>"
    return [head, *lines, foot]


def render(teams: list[TeamRow], limit: int = 3800) -> list[str]:
    """Страницы текста (HTML), каждая укладывается в одно сообщение Telegram."""
    if not teams:
        return ["ℹ️ Пока нет ни одной команды."]
    pages, cur = [], ""
    for block in _blocks(teams):
        if cur and len(cur) + len(block) + 1 > limit:
            pages.append(cur)
            cur = block
        else:
            cur = f"{cur}\n{block}" if cur else block
    return pages + [cur]
