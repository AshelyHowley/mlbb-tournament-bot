"""Deputy appointment: secure one-candidate invite, main-admin-only approval, max 2 active, and the
broad-but-bounded staff permissions (everything except managing other deputies)."""
import asyncio

# import asyncpg  # removed for SQLite
import pytest

from app.services.deputy import DeputyService
from app.services.errors import DeputyInviteClaimed, DeputyLimitReached, NotFound
from app.services.registration import TgUser
from app.services.roles import RoleService
from app.utils.text import INVITE_TOKEN_RE
from tests.helpers import add_user

pytestmark = pytest.mark.db

MAIN = 999
CAND1 = TgUser(101, "cand0101", "Candidate One")
CAND2 = TgUser(102, "cand0102", "Candidate Two")
CAND3 = TgUser(103, "cand0103", "Candidate Three")


@pytest.fixture(autouse=True)
async def _main_admin_exists(pool):
    """In production the main admin's `users` row always exists before they can reach a handler
    (UserTrackMiddleware upserts on every update); calling the service directly needs the same."""
    await add_user(pool, MAIN, "theadmin")


class Cfg:
    def __init__(self, admin_id):
        self.admin_id = admin_id

    def is_bot_admin(self, uid):
        return uid == self.admin_id


async def test_invite_token_is_secure_and_unique(pool):
    svc = DeputyService(pool)
    tokens = {await svc.create_invite(MAIN) for _ in range(10)}
    assert len(tokens) == 10
    assert all(INVITE_TOKEN_RE.fullmatch(t) and len(t) >= 32 for t in tokens)
    async with pool.acquire() as c:
        own_token = await c.fetchval("SELECT token FROM deputy_invites WHERE id = 1")
        other_token = next(t for t in tokens if t != own_token)  # a token that provably belongs to a different row
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("UPDATE deputy_invites SET token = $1 WHERE id = 1", other_token)


async def test_one_token_one_candidate(pool):
    svc = DeputyService(pool)
    token = await svc.create_invite(MAIN)
    assert (await svc.peek(token, CAND1.id)).status == "ok"
    inv = await svc.claim(CAND1, token)
    assert inv["candidate_id"] == CAND1.id and inv["status"] == "claimed"
    assert (await svc.peek(token, CAND1.id)).status == "mine"
    assert (await svc.peek(token, CAND2.id)).status == "claimed_by_other"
    with pytest.raises(DeputyInviteClaimed):
        await svc.claim(CAND2, token)
    again = await svc.claim(CAND1, token)  # the same candidate re-clicking is idempotent
    assert again["candidate_id"] == CAND1.id


async def test_ten_concurrent_claims_exactly_one_candidate(pool):
    svc = DeputyService(pool)
    token = await svc.create_invite(MAIN)
    candidates = [TgUser(200 + i, f"user{200 + i:04d}", f"U{i}") for i in range(10)]
    res = await asyncio.gather(*[svc.claim(u, token) for u in candidates], return_exceptions=True)
    winners = {r["candidate_id"] for r in res if not isinstance(r, BaseException)}
    assert len(winners) == 1
    assert all(isinstance(r, DeputyInviteClaimed) for r in res if isinstance(r, BaseException))


async def test_invalid_and_unknown_tokens(pool):
    svc = DeputyService(pool)
    for bad in ("", "short", "x" * 49, None, "'; DROP TABLE deputies;--" + "a" * 20):
        assert (await svc.peek(bad, CAND1.id)).status == "invalid"
        with pytest.raises(NotFound):
            await svc.claim(CAND1, bad)
    assert (await svc.peek("A" * 32, CAND1.id)).status == "invalid"
    with pytest.raises(NotFound):
        await svc.claim(CAND1, "A" * 32)


async def test_no_rights_before_main_admin_approval(pool):
    svc, roles = DeputyService(pool), RoleService(pool, Cfg(MAIN))
    token = await svc.create_invite(MAIN)
    await svc.claim(CAND1, token)
    assert not await roles.is_deputy(CAND1.id) and not await roles.is_staff(CAND1.id)
    assert await svc.pending() and (await svc.pending())[0]["candidate_id"] == CAND1.id


async def test_main_admin_approve_and_reject(pool):
    svc, roles = DeputyService(pool), RoleService(pool, Cfg(MAIN))
    t1, t2 = await svc.create_invite(MAIN), await svc.create_invite(MAIN)
    inv1 = await svc.claim(CAND1, t1)
    inv2 = await svc.claim(CAND2, t2)
    await svc.approve(MAIN, inv1["id"])
    await svc.reject(MAIN, inv2["id"])
    assert await roles.is_deputy(CAND1.id) and await roles.is_staff(CAND1.id)
    assert not await roles.is_deputy(CAND2.id)
    active = await svc.list_active()
    assert [a["user_id"] for a in active] == [CAND1.id]
    with pytest.raises(NotFound):  # already decided
        await svc.approve(MAIN, inv2["id"])


async def test_max_two_active_deputies_db_enforced(pool):
    svc = DeputyService(pool)
    for cand in (CAND1, CAND2):
        token = await svc.create_invite(MAIN)
        inv = await svc.claim(cand, token)
        await svc.approve(MAIN, inv["id"])
    assert len(await svc.list_active()) == 2
    with pytest.raises(DeputyLimitReached):
        await svc.create_invite(MAIN)  # UI-level pre-check
    async with pool.acquire() as c:  # DB-level enforcement even bypassing the service
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (103, 'x')")
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO deputies (user_id, approved_by) VALUES (103, $1)", MAIN)
        assert e.value.sqlstate == "MB008"


async def test_concurrent_approvals_never_exceed_two(pool):
    svc = DeputyService(pool)
    invites = []
    for i in range(5):
        cand = TgUser(300 + i, f"race{i:04d}", f"R{i}")
        token = await svc.create_invite(MAIN)
        invites.append(await svc.claim(cand, token))
    res = await asyncio.gather(*[svc.approve(MAIN, inv["id"]) for inv in invites], return_exceptions=True)
    ok = [r for r in res if not isinstance(r, BaseException)]
    assert len(ok) == 2
    assert all(isinstance(r, DeputyLimitReached) for r in res if isinstance(r, BaseException))
    assert len(await svc.list_active()) == 2


async def test_revoke_is_immediate_and_persists(pool):
    svc, roles = DeputyService(pool), RoleService(pool, Cfg(MAIN))
    token = await svc.create_invite(MAIN)
    inv = await svc.claim(CAND1, token)
    await svc.approve(MAIN, inv["id"])
    assert await roles.is_deputy(CAND1.id)
    await svc.revoke(MAIN, CAND1.id)
    assert not await roles.is_deputy(CAND1.id) and not await roles.is_staff(CAND1.id)
    with pytest.raises(NotFound):
        await svc.revoke(MAIN, CAND1.id)  # already revoked
    # the slot is free again
    token2 = await svc.create_invite(MAIN)
    inv2 = await svc.claim(CAND2, token2)
    await svc.approve(MAIN, inv2["id"])
    assert await roles.is_deputy(CAND2.id)


async def test_main_admin_is_never_counted_as_deputy(pool):
    roles = RoleService(pool, Cfg(MAIN))
    assert roles.is_main_admin(MAIN) and await roles.is_staff(MAIN) and not await roles.is_deputy(MAIN)


async def test_roles_persist_a_fresh_service_instance(pool):
    """Simulates surviving a bot restart: a new RoleService/DeputyService still sees the same deputy."""
    svc = DeputyService(pool)
    token = await svc.create_invite(MAIN)
    inv = await svc.claim(CAND1, token)
    await svc.approve(MAIN, inv["id"])
    fresh_roles = RoleService(pool, Cfg(MAIN))
    assert await fresh_roles.is_deputy(CAND1.id)
