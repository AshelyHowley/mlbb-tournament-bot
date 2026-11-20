"""«Список команд» для админа: все команды активного турнира с названием и звёздами в трёх оформлениях.

Звёзды команды - сумма рангов её игроков (как «Суммарный ранг» в карточке команды)."""
from __future__ import annotations

from dataclasses import dataclass

from app.utils.text import esc

STYLES = {
    "cup": "🏆 Турнирный",
    "table": "📋 Таблица",
    "cards": "✨ Карточки",
}

_SQL = """
    SELECT t.id, t.name,
           COALESCE(SUM(m.rank), 0) AS stars,
           COUNT(m.user_id)         AS players
      FROM teams t LEFT JOIN team_members m ON m.team_id = t.id
     WHERE t.tournament_id = (SELECT id FROM tournaments WHERE status = 'active')
     GROUP BY t.id
     ORDER BY t.created_at, t.id
"""


@dataclass(frozen=True)
class TeamRow:
    name: str
    stars: int
    players: int


async def load(pool) -> list[TeamRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SQL)
    return [TeamRow(r["name"], int(r["stars"] or 0), int(r["players"] or 0)) for r in rows]


def _players(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "игрок"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "игрока"
    else:
        word = "игроков"
    return f"{n} {word}"


def _cup(teams: list[TeamRow]) -> list[str]:
    """По убыванию звёзд, медали тройке лидеров."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    ranked = sorted(teams, key=lambda t: -t.stars)
    head = "🏆 <b>УЧАСТНИКИ ТУРНИРА</b> 🏆\n━━━━━━━━━━━━━━━━━━"
    lines = [f"{medals.get(i, f'<b>{i}.</b>')} <b>{esc(t.name)}</b> — ⭐ {t.stars}" for i, t in enumerate(ranked, 1)]
    total = sum(t.stars for t in teams)
    foot = f"━━━━━━━━━━━━━━━━━━\n👥 Команд: <b>{len(teams)}</b>  ·  ⭐ Всего звёзд: <b>{total}</b>"
    return [head, *lines, foot]


def _table(teams: list[TeamRow]) -> list[str]:
    """Моноширинная таблица в порядке регистрации."""
    width = min(max([len(t.name) for t in teams] + [7]), 20)
    num_w = len(str(len(teams)))
    star_w = max([len(str(t.stars)) for t in teams] + [2])

    def row(n: str, name: str, stars: str) -> str:
        return f"{n:>{num_w}} │ {name:<{width}} │ {stars:>{star_w}}"

    def cut(name: str) -> str:
        return name if len(name) <= width else name[: width - 1] + "…"

    sep = "─" * num_w + "─┼─" + "─" * width + "─┼─" + "─" * star_w
    body = [row("#", "Команда", "⭐"), sep]
    body += [row(str(i), cut(t.name), str(t.stars)) for i, t in enumerate(teams, 1)]
    total = sum(t.stars for t in teams)
    body += [sep, row("", "Итого", str(total))]
    # таблица режется на куски по строкам, каждый кусок - свой <pre>
    step = 60
    chunks = [body[i:i + step] for i in range(0, len(body), step)]
    return ["📋 <b>Список команд</b>"] + ["<pre>" + esc(chr(10).join(c)) + "</pre>" for c in chunks]


def _cards(teams: list[TeamRow]) -> list[str]:
    """Карточка на команду, по алфавиту, со средним числом звёзд."""
    ordered = sorted(teams, key=lambda t: t.name.casefold())
    avg = round(sum(t.stars for t in teams) / len(teams)) if teams else 0
    head = f"✨ <b>Команды турнира</b> ✨\n<i>Команд: {len(teams)} · в среднем ⭐ {avg}</i>"
    cards = [f"\n┏ <b>{esc(t.name)}</b>\n┗ ⭐ <b>{t.stars}</b>  ·  👥 {_players(t.players)}" for t in ordered]
    return [head, *cards]


_RENDER = {"cup": _cup, "table": _table, "cards": _cards}


def render(style: str, teams: list[TeamRow], limit: int = 3800) -> list[str]:
    """Страницы текста (HTML) для выбранного оформления, каждая укладывается в одно сообщение Telegram."""
    if not teams:
        return ["ℹ️ Пока нет ни одной команды."]
    pages, cur = [], ""
    for block in _RENDER[style](teams):
        if cur and len(cur) + len(block) + 1 > limit:
            pages.append(cur)
            cur = block.lstrip("\n")
        else:
            cur = f"{cur}\n{block}" if cur else block
    return pages + [cur]
