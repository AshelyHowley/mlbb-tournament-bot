"""PostgreSQL itself must protect the business invariants (bypassing the Python service)."""
# import asyncpg  # removed for SQLite
import pytest

pytestmark = pytest.mark.db


async def user(c, uid, name=None):
    await c.execute(
        "INSERT INTO users (telegram_id, username, username_norm, full_name) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
        uid, name, name.lower() if name else None, f"User {uid}",
    )


async def team_raw(c, cap_id, name, others=()):
    """Raw inserts, no service code. Must run in ONE transaction (roster is validated at COMMIT)."""
    await user(c, cap_id, f"cap{cap_id:05d}")
    tid = await c.fetchval(
        "INSERT INTO teams (name, name_norm, captain_id, created_by, invite_token) VALUES ($1,$2,$3,$3,$4) RETURNING id",
        name, name.casefold(), cap_id, f"tok{cap_id:05d}" + "x" * 30,
    )
    await c.execute("INSERT INTO team_members (team_id, user_id, is_captain) VALUES ($1,$2,TRUE)", tid, cap_id)
    for uid in others:
        await user(c, uid)
        await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1,$2)", tid, uid)
    return tid


async def test_valid_team_commits_with_one_to_five_members(pool):
    async with pool.acquire() as c, c.transaction():
        await team_raw(c, 1, "Alpha", [11, 12, 13, 14])
        await team_raw(c, 2, "Solo Captain")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM team_members") == 6


async def test_sixth_member_rejected_by_database(pool):
    async with pool.acquire() as c, c.transaction():
        tid = await team_raw(c, 1, "Alpha", [11, 12, 13, 14])
    async with pool.acquire() as c:
        await user(c, 99)
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("INSERT INTO team_members (team_id, user_id) VALUES ($1, 99)", tid)
        assert e.value.sqlstate == "MB003"
        assert await c.fetchval("SELECT count(*) FROM team_members") == 5


async def test_team_without_its_captain_member_rejected_at_commit(pool):
    with pytest.raises(asyncpg.PostgresError) as e:
        async with pool.acquire() as c, c.transaction():
            await user(c, 1, "captain1")
            await c.execute("INSERT INTO teams (name,name_norm,captain_id,created_by,invite_token) VALUES ('Alpha','alpha',1,1,$1)", "a" * 32)
    assert e.value.sqlstate == "MB002"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM teams") == 0


async def test_captain_flag_must_match_teams_captain_id(pool):
    with pytest.raises(asyncpg.PostgresError) as e:
        async with pool.acquire() as c, c.transaction():
            await user(c, 1, "captain1")
            await user(c, 2, "other001")
            tid = await c.fetchval("INSERT INTO teams (name,name_norm,captain_id,created_by,invite_token) VALUES ('Alpha','alpha',1,1,$1) RETURNING id", "b" * 32)
            await c.execute("INSERT INTO team_members (team_id,user_id,is_captain) VALUES ($1,2,TRUE)", tid)  # wrong person
    assert e.value.sqlstate == "MB002"


async def test_only_one_captain_row_per_team(pool):
    with pytest.raises(asyncpg.UniqueViolationError):
        async with pool.acquire() as c, c.transaction():
            tid = await team_raw(c, 1, "Alpha")
            await user(c, 2)
            await c.execute("INSERT INTO team_members (team_id,user_id,is_captain) VALUES ($1,2,TRUE)", tid)


async def test_one_user_one_team(pool):
    async with pool.acquire() as c, c.transaction():
        await team_raw(c, 1, "Alpha", [11])
    with pytest.raises(asyncpg.UniqueViolationError) as e:
        async with pool.acquire() as c, c.transaction():
            await team_raw(c, 2, "Beta", [11])  # 11 already plays for Alpha
    assert e.value.constraint_name == "team_members_user_uq"
    with pytest.raises(asyncpg.UniqueViolationError):
        async with pool.acquire() as c, c.transaction():
            await team_raw(c, 11, "Gamma")  # a member cannot captain another team


async def test_duplicate_team_name_and_case(pool):
    async with pool.acquire() as c, c.transaction():
        await team_raw(c, 1, "Alpha")
    with pytest.raises(asyncpg.UniqueViolationError) as e:
        async with pool.acquire() as c, c.transaction():
            await team_raw(c, 2, "ALPHA")
    assert e.value.constraint_name == "teams_name_norm_uq"


async def test_duplicate_captain(pool):
    async with pool.acquire() as c, c.transaction():
        await team_raw(c, 1, "Alpha")
    with pytest.raises(asyncpg.UniqueViolationError) as e:
        async with pool.acquire() as c, c.transaction():
            await c.execute("INSERT INTO teams (name,name_norm,captain_id,created_by,invite_token) VALUES ('Bravo','bravo',1,1,$1)", "c" * 32)
    assert e.value.constraint_name == "teams_captain_uq"


@pytest.mark.parametrize("token", ["short", "x" * 49, "has space" + "a" * 30, "юникод" * 8, "a" * 31 + "!"])
async def test_invite_token_format_check(pool, token):
    async with pool.acquire() as c:
        await user(c, 1, "captain1")
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO teams (name,name_norm,captain_id,created_by,invite_token) VALUES ('Alpha','alpha',1,1,$1)", token)


async def test_removing_a_regular_member_is_allowed_captain_is_not(pool):
    async with pool.acquire() as c, c.transaction():
        await team_raw(c, 1, "Alpha", [11, 12])
    async with pool.acquire() as c:
        await c.execute("DELETE FROM team_members WHERE user_id = 12")
        with pytest.raises(asyncpg.PostgresError):
            await c.execute("DELETE FROM team_members WHERE is_captain")
        assert await c.fetchval("SELECT count(*) FROM team_members") == 2


async def test_membership_cannot_be_reassigned(pool):
    async with pool.acquire() as c, c.transaction():
        await team_raw(c, 1, "Alpha", [11])
        await team_raw(c, 2, "Beta")
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.PostgresError) as e:
            await c.execute("UPDATE team_members SET team_id = (SELECT id FROM teams WHERE name = 'Beta') WHERE user_id = 11")
        assert e.value.sqlstate == "MB004"


async def test_username_check_constraint(pool):
    with pytest.raises(asyncpg.CheckViolationError):
        async with pool.acquire() as c:
            await c.execute("INSERT INTO users (telegram_id, username_norm) VALUES (1, 'Bad Name')")


async def test_users_and_solo_accept_null_username(pool):
    async with pool.acquire() as c:
        await c.execute("INSERT INTO users (telegram_id, full_name) VALUES (1, 'NoUsername')")
        await c.execute("INSERT INTO solo_players (user_id, role) VALUES (1, 'EXP')")
        assert await c.fetchval("SELECT username FROM solo_players") is None


async def test_solo_duplicates_and_roles(pool):
    async with pool.acquire() as c:
        await user(c, 1, "solo0001")
        await c.execute("INSERT INTO solo_players (user_id,username,username_norm,role) VALUES (1,'solo0001','solo0001','EXP')")
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("INSERT INTO solo_players (user_id,username,username_norm,role) VALUES (1,'solo0001','solo0001','MID')")
        await user(c, 2, "solo0002")
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO solo_players (user_id,username,username_norm,role) VALUES (2,'solo0002','solo0002','SUPPORT')")


async def _two_teams(pool):
    async with pool.acquire() as c, c.transaction():
        a = await team_raw(c, 1, "Alpha", [11, 12, 13, 14])
        b = await team_raw(c, 2, "Beta", [21, 22, 23, 24])
        t = await team_raw(c, 3, "Gamma", [31, 32, 33, 34])
    return a, b, t


async def test_match_constraints(pool):
    a, b, t = await _two_teams(pool)
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.CheckViolationError):  # self match
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8',$1,$1)", a)
        with pytest.raises(asyncpg.CheckViolationError):  # winner not participant
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id,winner_id,status) VALUES ('r8',$1,$2,$3,'finished')", a, b, t)
        with pytest.raises(asyncpg.CheckViolationError):  # finished without winner
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id,status) VALUES ('r8',$1,$2,'finished')", a, b)
        with pytest.raises(asyncpg.CheckViolationError):  # winner while scheduled
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id,winner_id) VALUES ('r8',$1,$2,$1)", a, b)
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # unknown stage
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('nope',$1,$2)", a, b)
        await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8',$1,$2)", a, b)
        with pytest.raises(asyncpg.UniqueViolationError):  # same pair reversed, same stage
            await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8',$1,$2)", b, a)
        await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('final',$1,$2)", b, a)  # other stage OK
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("UPDATE matches SET winner_id=$1, status='finished' WHERE stage_code='r8'", t)


async def test_team_with_matches_cannot_be_deleted_but_members_cascade(pool):
    a, b, t = await _two_teams(pool)
    async with pool.acquire() as c:
        await c.execute("INSERT INTO matches (stage_code,team_a_id,team_b_id) VALUES ('r8',$1,$2)", a, b)
        with pytest.raises(asyncpg.RestrictViolationError):
            await c.execute("DELETE FROM teams WHERE id=$1", a)
        await c.execute("DELETE FROM teams WHERE id=$1", t)
        assert await c.fetchval("SELECT count(*) FROM team_members WHERE team_id=$1", t) == 0


async def test_broadcast_constraints(pool):
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO broadcasts (admin_id,audience,content_type,text) VALUES (1,'everyone','text','x')")
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO broadcasts (admin_id,audience,content_type) VALUES (1,'all','photo')")


async def test_all_fk_columns_indexed(pool):
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            SELECT con.conrelid::regclass::text AS tbl, a.attname AS col
              FROM pg_constraint con
              JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
             WHERE con.contype = 'f' AND con.connamespace = current_schema()::regnamespace
               AND NOT EXISTS (
                   SELECT 1 FROM pg_index i WHERE i.indrelid = con.conrelid AND i.indkey[0] = con.conkey[1])
            """
        )
    assert [tuple(r) for r in rows] == []
