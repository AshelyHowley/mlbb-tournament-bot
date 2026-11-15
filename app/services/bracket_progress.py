"""Фиксация победителя пары и продвижение по сетке (кнопка «🏆» в редакторе сетки).

Вызывать внутри транзакции: при любой ошибке ничего не должно записаться наполовину.

Правила:
  * победитель обязан быть одним из двух участников пары;
  * победитель проходит в слот следующего матча (next_match_id);
  * результат можно исправить, пока следующий матч не сыгран: прежний победитель
    в следующем матче заменяется новым, а не остаётся там «навсегда»;
  * когда оба полуфинала сыграны, проигравшие автоматически ставятся в матч за 3-е место.
"""
from __future__ import annotations

from app.services.errors import Conflict, NotFound, ValidationError


async def record_winner(conn, match_id: int, winner_id: int) -> bool:
    """Записывает победителя. Возвращает False, если этот победитель уже был записан (ничего не меняется)."""
    m = await conn.fetchrow(
        "SELECT id, tournament_id, stage_code, team_a_id, team_b_id, winner_id, status, next_match_id "
        "FROM matches WHERE id = $1",
        match_id,
    )
    if not m:
        raise NotFound("Матч")
    if m["team_a_id"] is None or m["team_b_id"] is None:
        raise ValidationError("⚠️ В паре ещё нет обоих участников.")
    if winner_id not in (m["team_a_id"], m["team_b_id"]):
        raise ValidationError("⚠️ Эта команда не играет в этой паре.")
    old = m["winner_id"] if m["status"] == "finished" else None
    if old == winner_id:
        return False

    if m["next_match_id"]:
        await _advance(conn, m["next_match_id"], old, winner_id)

    await conn.execute(
        "UPDATE matches SET winner_id = $2, status = 'finished', finished_at = datetime('now') WHERE id = $1",
        match_id, winner_id,
    )
    if m["stage_code"] == "semi":
        await _sync_third_place(conn, m["tournament_id"])
    return True


async def _advance(conn, next_id: int, old_winner, new_winner: int) -> None:
    nm = await conn.fetchrow(
        "SELECT stage_code, team_a_id, team_b_id, status FROM matches WHERE id = $1", next_id
    )
    if not nm:
        return
    if old_winner is not None and old_winner in (nm["team_a_id"], nm["team_b_id"]):
        if nm["status"] == "finished" and nm["stage_code"] != "finalist":
            raise Conflict("⚠️ Следующий матч уже сыгран. Сначала исправьте его результат.")
        col = "team_a_id" if nm["team_a_id"] == old_winner else "team_b_id"
        await conn.execute(f"UPDATE matches SET {col} = $2 WHERE id = $1", next_id, new_winner)
        return
    if new_winner in (nm["team_a_id"], nm["team_b_id"]):
        return
    if nm["team_a_id"] is None:
        await conn.execute("UPDATE matches SET team_a_id = $2 WHERE id = $1", next_id, new_winner)
    elif nm["team_b_id"] is None:
        await conn.execute("UPDATE matches SET team_b_id = $2 WHERE id = $1", next_id, new_winner)


def _loser(m) -> int:
    return m["team_b_id"] if m["winner_id"] == m["team_a_id"] else m["team_a_id"]


async def _sync_third_place(conn, tournament_id: int) -> None:
    semis = await conn.fetch(
        "SELECT team_a_id, team_b_id, winner_id, status FROM matches "
        "WHERE tournament_id = $1 AND stage_code = 'semi' ORDER BY id",
        tournament_id,
    )
    if len(semis) != 2 or any(s["status"] != "finished" for s in semis):
        return
    a, b = _loser(semis[0]), _loser(semis[1])
    third = await conn.fetchrow(
        "SELECT id, team_a_id, team_b_id, status FROM matches WHERE tournament_id = $1 AND stage_code = 'third'",
        tournament_id,
    )
    if third is None:
        await conn.execute(
            "INSERT INTO matches (tournament_id, stage_code, team_a_id, team_b_id, status) "
            "VALUES ($1, 'third', $2, $3, 'pending')",
            tournament_id, a, b,
        )
        return
    if {third["team_a_id"], third["team_b_id"]} == {a, b}:
        return
    if third["status"] == "finished":
        raise Conflict("⚠️ Матч за 3-е место уже сыгран. Сначала удалите его или исправьте результат.")
    await conn.execute(
        "UPDATE matches SET team_a_id = $2, team_b_id = $3 WHERE id = $1", third["id"], a, b
    )
