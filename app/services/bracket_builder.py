"""Построение сетки Single Elimination: чистая функция без БД, детерминированная по seed.

Что гарантируется (и проверено в tests/test_bracket_builder.py):
  * ровно n-1 матч для n команд (никаких лишних раундов);
  * bye раздаются равномерно по сетке и никогда не встречаются друг с другом;
  * команда с bye сразу стоит в слоте матча 2-го раунда, а не "выигрывает" фантомный матч;
  * связи next_match задают ровно дерево, ведущее в финал.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Sequence

# число матчей в раунде -> stage_code (как в таблице stages)
_STAGE_BY_MATCHES = {1: "final", 2: "semi", 4: "r4", 8: "r8", 16: "r16", 32: "r32"}


@dataclass
class PlannedMatch:
    round: int                       # 1 = первый раунд
    idx: int                         # позиция в раунде сверху вниз
    stage: str
    team_a: Optional[int] = None
    team_b: Optional[int] = None
    next: Optional[tuple[int, int]] = None   # (round, idx) следующего матча


def _bit_reverse_order(count: int) -> list[int]:
    """0..count-1 в порядке, при котором первые k элементов максимально разбросаны.
    Для count=4: [0, 2, 1, 3]. Так bye не копятся в одной половине сетки."""
    bits = max((count - 1).bit_length(), 1) if count > 1 else 0
    if count <= 1:
        return list(range(count))
    return sorted(range(count), key=lambda i: int(format(i, f"0{bits}b")[::-1], 2))


def plan_bracket(team_ids: Sequence[int], seed: int | None = None) -> list[PlannedMatch]:
    teams = list(dict.fromkeys(team_ids))          # убираем дубли, порядок сохраняем
    n = len(teams)
    if n < 2:
        raise ValueError("Для сетки нужно минимум 2 команды")
    if n > 64:
        raise ValueError("Поддерживается до 64 команд")

    rng = random.Random(seed)                      # свой генератор: не трогает глобальный random
    rng.shuffle(teams)

    size = 1 << (n - 1).bit_length()               # ближайшая степень двойки >= n
    rounds = size.bit_length() - 1
    byes = size - n
    pairs = size // 2

    matches: dict[tuple[int, int], PlannedMatch] = {}
    for r in range(1, rounds + 1):
        count = size >> r
        for i in range(count):
            nxt = (r + 1, i // 2) if r < rounds else None
            matches[(r, i)] = PlannedMatch(r, i, _STAGE_BY_MATCHES[count], next=nxt)

    bye_pairs = set(_bit_reverse_order(pairs)[:byes])
    it = iter(teams)
    for p in range(pairs):
        if p in bye_pairs:
            # команда проходит сразу во 2-й раунд, матча в 1-м раунде нет
            team = next(it)
            nxt = matches[(2, p // 2)]
            if p % 2 == 0:
                nxt.team_a = team
            else:
                nxt.team_b = team
            del matches[(1, p)]
        else:
            m = matches[(1, p)]
            m.team_a, m.team_b = next(it), next(it)

    return sorted(matches.values(), key=lambda m: (-m.round, m.idx))   # от финала к первому раунду


async def save_plan(conn, tournament_id: int, plan: list[PlannedMatch]) -> dict[tuple[int, int], int]:
    """Записывает план в таблицу matches. Вставляем от финала к первому раунду,
    чтобы next_match_id был известен сразу, а id детей шли по возрастанию idx."""
    await conn.execute("UPDATE matches SET next_match_id = NULL WHERE tournament_id = $1", tournament_id)
    await conn.execute("DELETE FROM matches WHERE tournament_id = $1", tournament_id)

    # коробка «Финалист» (чемпион) — как в вашем текущем формате
    await conn.execute(
        "INSERT INTO matches (tournament_id, stage_code, status) VALUES ($1, 'finalist', 'pending')",
        tournament_id,
    )
    finalist_id = await conn.fetchval("SELECT last_insert_rowid()")

    ids: dict[tuple[int, int], int] = {}
    for m in plan:                                    # plan уже отсортирован от финала
        next_id = finalist_id if m.next is None else ids[m.next]
        await conn.execute(
            "INSERT INTO matches (tournament_id, stage_code, team_a_id, team_b_id, status, next_match_id) "
            "VALUES ($1, $2, $3, $4, 'pending', $5)",
            tournament_id, m.stage, m.team_a, m.team_b, next_id,
        )
        ids[(m.round, m.idx)] = await conn.fetchval("SELECT last_insert_rowid()")
    return ids
