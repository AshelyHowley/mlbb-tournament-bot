import itertools
import pytest
from app.services.bracket_builder import plan_bracket

SIZES = list(range(2, 65))


def real_matches(plan):
    return [m for m in plan]


@pytest.mark.parametrize("n", SIZES)
def test_match_count_and_all_teams_placed(n):
    teams = list(range(1, n + 1))
    plan = plan_bracket(teams, seed=n)
    assert len(plan) == n - 1                      # главное свойство single elimination
    placed = [t for m in plan for t in (m.team_a, m.team_b) if t is not None]
    assert sorted(placed) == teams                 # каждая команда ровно один раз, никто не потерян


@pytest.mark.parametrize("n", SIZES)
def test_tree_leads_to_single_final(n):
    plan = plan_bracket(range(n), seed=1)
    finals = [m for m in plan if m.next is None]
    assert len(finals) == 1 and finals[0].stage == "final"
    keys = {(m.round, m.idx) for m in plan}
    for m in plan:
        assert m.next is None or m.next in keys


@pytest.mark.parametrize("n", SIZES)
def test_byes_are_spread_evenly_between_halves(n):
    plan = plan_bracket(range(n), seed=2)
    size = 1 << (n - 1).bit_length()
    r2 = [m for m in plan if m.round == 2]
    if len(r2) < 2:
        return
    half = len(r2) // 2
    preset = lambda ms: sum((m.team_a is not None) + (m.team_b is not None) for m in ms)
    top, bottom = preset([m for m in r2 if m.idx < half]), preset([m for m in r2 if m.idx >= half])
    assert preset(r2) == size - n                  # в R2 заранее стоят только bye-команды
    assert abs(top - bottom) <= 1


def simulate(plan, rng_seed=0):
    """Играем весь турнир по правилам bracket_win: победитель встаёт в первый пустой слот."""
    import random
    rng = random.Random(rng_seed)
    state = {(m.round, m.idx): [m.team_a, m.team_b] for m in plan}
    nxt = {(m.round, m.idx): m.next for m in plan}
    for key in sorted(state, key=lambda k: (k[0], k[1])):      # раунд за раундом
        a, b = state[key]
        assert a is not None and b is not None, f"матч {key} сыгран без соперника"
        w = rng.choice([a, b])
        if nxt[key]:
            slot = state[nxt[key]]
            if slot[0] is None:
                slot[0] = w
            else:
                assert slot[1] is None, "слот занят"
                slot[1] = w
        else:
            return w


@pytest.mark.parametrize("n", SIZES)
def test_full_tournament_plays_out(n):
    plan = plan_bracket(range(n), seed=n)
    champion = simulate(plan, rng_seed=n)
    assert champion in range(n)


def test_same_seed_same_bracket_different_seed_differs():
    a = plan_bracket(range(16), seed=7)
    b = plan_bracket(range(16), seed=7)
    c = plan_bracket(range(16), seed=8)
    assert [(m.team_a, m.team_b) for m in a] == [(m.team_a, m.team_b) for m in b]
    assert [(m.team_a, m.team_b) for m in a] != [(m.team_a, m.team_b) for m in c]


def test_input_not_mutated_and_dupes_removed():
    src = [3, 1, 2, 2, 5]
    plan_bracket(src, seed=1)
    assert src == [3, 1, 2, 2, 5]
    assert len([t for m in plan_bracket(src, seed=1) for t in (m.team_a, m.team_b) if t is not None]) == 4


def test_too_few_teams():
    with pytest.raises(ValueError):
        plan_bracket([1])
