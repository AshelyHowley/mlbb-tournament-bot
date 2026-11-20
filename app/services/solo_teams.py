"""Assembling full teams out of solo players (admin button «Собрать команды из соло»).

Rules:
- only full teams of 5 are built; whoever does not fit stays solo and is reported to the admin;
- every team gets one player per role (EXP / GOLD / MID / JUNGLE / ROAM), using the roles the players
  applied for ("ALL" fits anywhere); when the roles cannot cover every slot, the remaining slots are
  filled off-role and flagged;
- among players competing for places, the earlier registration wins;
- stars are spread so the teams' star totals are as even as possible;
- the captain is the player with the most stars (earlier registration on a tie);
- players KEEP their solo registration: the teams are flagged teams.from_solo and a new assembly
  disbands them and builds new ones.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.repositories import teams as teams_repo
from app.services.registration import ROLES
from app.utils.audit import log_registration
from app.utils.text import new_invite_token

TEAM_SIZE = len(ROLES)
_ACTIVE = "(SELECT id FROM tournaments WHERE status = 'active')"

TEAM_NAMES = (
    "Северный Ветер", "Тёмные Лисы", "Стальные Волки", "Огненный Феникс", "Ночные Совы",
    "Ледяные Драконы", "Багровый Рассвет", "Громовержцы", "Песчаная Буря", "Чёрные Пантеры",
    "Небесный Щит", "Серебряные Стрелы", "Дикие Тигры", "Звёздный Легион", "Призрачный Клинок",
    "Алый Шторм", "Каменные Титаны", "Золотые Орлы", "Лунные Охотники", "Пламя Востока",
    "Shadow Wolves", "Iron Phoenix", "Storm Breakers", "Night Owls", "Frost Dragons",
    "Crimson Dawn", "Thunder Strike", "Sand Vipers", "Black Panthers", "Sky Guardians",
    "Silver Arrows", "Wild Tigers", "Star Legion", "Ghost Blades", "Red Tempest",
    "Stone Titans", "Golden Eagles", "Moon Hunters", "Eastern Flame", "Void Walkers",
)


@dataclass(frozen=True)
class SoloPlayer:
    user_id: int
    username: Optional[str]
    full_name: str
    roles: frozenset  # roles the player can take; "ALL"/empty -> every role
    stars: int
    order: int  # registration order, lower = earlier


@dataclass(frozen=True)
class Seat:
    player: SoloPlayer
    role: str
    off_role: bool = False  # the player did not apply for this role


@dataclass
class BuiltTeam:
    name: str
    seats: list  # list[Seat], captain first
    team_id: Optional[int] = None

    @property
    def captain(self) -> SoloPlayer:
        return self.seats[0].player

    @property
    def total(self) -> int:
        return sum(s.player.stars for s in self.seats)


@dataclass
class Plan:
    teams: list = field(default_factory=list)      # list[BuiltTeam]
    leftovers: list = field(default_factory=list)  # list[SoloPlayer]


def parse_roles(raw: Optional[str]) -> frozenset:
    roles = {r.strip().upper() for r in (raw or "").split(",") if r.strip()}
    if not roles or "ALL" in roles:
        return frozenset(ROLES)
    return frozenset(r for r in roles if r in ROLES) or frozenset(ROLES)


def _select(players: Sequence[SoloPlayer], n_teams: int):
    """Fill n_teams seats per role. Bipartite matching (Kuhn) in registration order: once a player
    gets a seat they keep one, so earlier players are never pushed out by later ones."""
    seats: dict[str, list[SoloPlayer]] = {r: [] for r in ROLES}

    def place(p: SoloPlayer, seen: set) -> bool:
        for r in ROLES:
            if r not in p.roles or r in seen:
                continue
            seen.add(r)
            if len(seats[r]) < n_teams:
                seats[r].append(p)
                return True
            for q in list(seats[r]):  # try to move a holder of this role elsewhere
                seats[r].remove(q)
                if place(q, seen):
                    seats[r].append(p)
                    return True
                seats[r].append(q)
        return False

    unplaced = [p for p in players if not place(p, set())]
    off_role: set[int] = set()
    leftovers = []
    for p in unplaced:  # roles could not cover every seat: fill the gaps off-role, earliest first
        free = next((r for r in ROLES if len(seats[r]) < n_teams), None)
        if free is None:
            leftovers.append(p)
            continue
        seats[free].append(p)
        off_role.add(p.user_id)
    return seats, off_role, leftovers


def _balance(seats: dict, n_teams: int) -> list[list[SoloPlayer]]:
    """One player per role in every team, star totals as even as possible: greedy start (the strongest
    remaining player goes to the weakest team), then same-role swaps while they reduce the spread."""
    teams: list[dict[str, SoloPlayer]] = [{} for _ in range(n_teams)]
    totals = [0] * n_teams
    spread = lambda r: max(p.stars for p in seats[r]) - min(p.stars for p in seats[r])  # noqa: E731
    for r in sorted(ROLES, key=spread, reverse=True):
        weakest_first = sorted(range(n_teams), key=lambda i: (totals[i], i))
        for i, p in zip(weakest_first, sorted(seats[r], key=lambda p: (-p.stars, p.order))):
            teams[i][r] = p
            totals[i] += p.stars
    improved = True
    for _ in range(200):
        if not improved:
            break
        improved = False
        for r in ROLES:
            for i in range(n_teams):
                for j in range(i + 1, n_teams):
                    a, b = teams[i][r].stars, teams[j][r].stars
                    d = a - b
                    # swapping changes totals by -d / +d; keep it if the sum of squares drops
                    if d and (totals[i] - d) ** 2 + (totals[j] + d) ** 2 < totals[i] ** 2 + totals[j] ** 2:
                        teams[i][r], teams[j][r] = teams[j][r], teams[i][r]
                        totals[i] -= d
                        totals[j] += d
                        improved = True
    return [[t[r] for r in ROLES] for t in teams]


def pick_names(count: int, taken_norms: set, rng: Optional[random.Random] = None) -> list[str]:
    rng = rng or random.Random()
    pool = [n for n in TEAM_NAMES if n.casefold() not in taken_norms]
    rng.shuffle(pool)
    names = pool[:count]
    n = 1
    while len(names) < count:
        name = f"Solo Team {n}"
        if name.casefold() not in taken_norms and name not in names:
            names.append(name)
        n += 1
    return names


def plan_teams(players: Sequence[SoloPlayer], taken_norms: set = frozenset(),
               rng: Optional[random.Random] = None) -> Plan:
    players = sorted(players, key=lambda p: p.order)
    n_teams = len(players) // TEAM_SIZE
    if n_teams == 0:
        return Plan([], list(players))
    seats, off_role, leftovers = _select(players, n_teams)
    names = pick_names(n_teams, set(taken_norms), rng)
    built = []
    for name, roster in zip(names, _balance(seats, n_teams)):
        seat_list = [Seat(p, r, p.user_id in off_role) for r, p in zip(ROLES, roster)]
        seat_list.sort(key=lambda s: (-s.player.stars, s.player.order))  # captain = most stars
        built.append(BuiltTeam(name, seat_list))
    return Plan(built, sorted(leftovers, key=lambda p: p.order))


# ---------------------------------------------------------------- database side
async def load_solos(conn) -> list[SoloPlayer]:
    rows = await conn.fetch(
        "SELECT s.user_id, COALESCE(u.username, s.username) AS username, "
        "       COALESCE(u.full_name, s.display_full_name) AS full_name, s.role, COALESCE(s.stars, 0) AS stars "
        "  FROM solo_players s JOIN users u ON u.telegram_id = s.user_id "
        f" WHERE s.tournament_id = {_ACTIVE} ORDER BY s.created_at, s.user_id"
    )
    return [SoloPlayer(r["user_id"], r["username"], r["full_name"] or "", parse_roles(r["role"]),
                       int(r["stars"] or 0), i) for i, r in enumerate(rows)]


async def preview(pool) -> dict:
    async with pool.acquire() as conn:
        solos = await conn.fetchval(f"SELECT count(*) FROM solo_players WHERE tournament_id = {_ACTIVE}")
        built = await conn.fetchval(f"SELECT count(*) FROM teams WHERE from_solo = 1 AND tournament_id = {_ACTIVE}")
        matches = await conn.fetchval(
            "SELECT count(*) FROM matches WHERE team_a_id IN (SELECT id FROM teams WHERE from_solo = 1 "
            f"AND tournament_id = {_ACTIVE}) OR team_b_id IN (SELECT id FROM teams WHERE from_solo = 1 "
            f"AND tournament_id = {_ACTIVE})"
        )
    return {"solos": solos, "teams": solos // TEAM_SIZE, "left": solos % TEAM_SIZE,
            "built": built, "matches": matches}


async def _disband(conn) -> int:
    ids = [r["id"] for r in await conn.fetch(f"SELECT id FROM teams WHERE from_solo = 1 AND tournament_id = {_ACTIVE}")]
    for tid in ids:  # same order as AdminService.delete_team
        await conn.execute(
            "UPDATE matches SET next_match_id = NULL WHERE next_match_id IN "
            "(SELECT id FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1)", tid,
        )
        await conn.execute("DELETE FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1", tid)
        t = await conn.fetchrow("SELECT name, captain_id FROM teams WHERE id = $1", tid)
        await log_registration(conn, t["captain_id"] or 0, "solo_team_disband", t["name"], team_id=tid)
        await conn.execute("DELETE FROM teams WHERE id = $1", tid)
    return len(ids)


async def assemble(pool, rng: Optional[random.Random] = None) -> tuple[Plan, int]:
    """Disbands the previous solo-built teams and builds new ones. Returns (plan, disbanded count)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            disbanded = await _disband(conn)
            taken = {r["name_norm"] for r in await conn.fetch(f"SELECT name_norm FROM teams WHERE tournament_id = {_ACTIVE}")}
            plan = plan_teams(await load_solos(conn), taken, rng)
            for team in plan.teams:
                cap = team.captain
                team.team_id = await teams_repo.insert_team(
                    conn, team.name, team.name.casefold(), cap.user_id, cap.user_id, new_invite_token(), None,
                    stars=team.total,
                )
                await conn.execute("UPDATE teams SET from_solo = 1 WHERE id = $1", team.team_id)
                for seat in team.seats:
                    is_cap = seat.player.user_id == cap.user_id
                    await teams_repo.add_member(conn, team.team_id, seat.player.user_id, is_captain=is_cap,
                                                rank=seat.player.stars)
                    await log_registration(conn, seat.player.user_id, "solo_team", team.name, team_id=team.team_id,
                                           member_role="captain" if is_cap else "member", roles=seat.role,
                                           stars=seat.player.stars)
    return plan, disbanded
