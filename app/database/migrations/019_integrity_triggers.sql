-- Business invariants enforced by the database itself (the PostgreSQL schema had them as
-- constraints / triggers; 001_sqlite dropped them, so only the Python services were checking).
-- Errors are raised as '<sqlstate>:<constraint>: <text>'; app/database/pool.py turns that prefix into
-- e.sqlstate / e.constraint_name, the same attributes asyncpg exposed.
-- SQLite has no commit-time (deferred) triggers, so every check here is immediate: the services
-- order their writes so that no intermediate state violates them.

-- ---------------------------------------------------------------- uniqueness
CREATE UNIQUE INDEX IF NOT EXISTS team_members_one_captain_uq ON team_members (team_id) WHERE is_captain = 1;
CREATE UNIQUE INDEX IF NOT EXISTS teams_captain_uq ON teams (captain_id, tournament_id) WHERE captain_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS teams_invite_token_uq ON teams (invite_token) WHERE invite_token IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS teams_captain_invite_token_uq ON teams (captain_invite_token) WHERE captain_invite_token IS NOT NULL;
-- the same pair (in either order) at most once per stage of a tournament; empty bracket slots are exempt
CREATE UNIQUE INDEX IF NOT EXISTS matches_pair_uq ON matches (tournament_id, stage_code, min(team_a_id, team_b_id), max(team_a_id, team_b_id))
    WHERE team_a_id IS NOT NULL AND team_b_id IS NOT NULL;

-- ---------------------------------------------------------------- team roster
CREATE TRIGGER IF NOT EXISTS team_members_guard_insert BEFORE INSERT ON team_members
BEGIN
    SELECT RAISE(ABORT, 'MB003:team_members_max5: a team has at most 5 players')
     WHERE (SELECT count(*) FROM team_members WHERE team_id = NEW.team_id) >= 5;
    SELECT RAISE(ABORT, '23505:team_members_user_uq: the user already plays for another team of this tournament')
     WHERE EXISTS (
        SELECT 1 FROM team_members m JOIN teams t ON t.id = m.team_id
         WHERE m.user_id = NEW.user_id AND m.team_id <> NEW.team_id
           AND t.tournament_id IS (SELECT tournament_id FROM teams WHERE id = NEW.team_id));
    SELECT RAISE(ABORT, 'MB001:solo_team_conflict: a solo player cannot join a team')
     WHERE EXISTS (
        SELECT 1 FROM solo_players s
         WHERE s.user_id = NEW.user_id
           AND s.tournament_id IS (SELECT tournament_id FROM teams WHERE id = NEW.team_id));
    SELECT RAISE(ABORT, 'MB002:team_members_captain_flag: the captain flag must match teams.captain_id')
     WHERE NEW.is_captain = 1 AND (SELECT captain_id FROM teams WHERE id = NEW.team_id) IS NOT NEW.user_id;
END;

CREATE TRIGGER IF NOT EXISTS team_members_no_move BEFORE UPDATE OF team_id, user_id ON team_members
WHEN NEW.team_id IS NOT OLD.team_id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT, 'MB004:team_members_immutable: a membership cannot be moved to another team or user');
END;

CREATE TRIGGER IF NOT EXISTS team_members_captain_flag_update BEFORE UPDATE OF is_captain ON team_members
WHEN NEW.is_captain = 1 AND OLD.is_captain = 0
BEGIN
    SELECT RAISE(ABORT, 'MB002:team_members_captain_flag: the captain flag must match teams.captain_id')
     WHERE (SELECT captain_id FROM teams WHERE id = NEW.team_id) IS NOT NEW.user_id;
END;

-- the captain's own row may only disappear together with the team (ON DELETE CASCADE)
CREATE TRIGGER IF NOT EXISTS team_members_keep_captain BEFORE DELETE ON team_members
WHEN OLD.is_captain = 1
BEGIN
    SELECT RAISE(ABORT, 'MB002:team_members_keep_captain: the captain row cannot be removed')
     WHERE EXISTS (SELECT 1 FROM teams WHERE id = OLD.team_id AND captain_id = OLD.user_id);
END;

-- a newly assigned captain must already be on the roster
CREATE TRIGGER IF NOT EXISTS teams_captain_is_member BEFORE UPDATE OF captain_id ON teams
WHEN NEW.captain_id IS NOT NULL AND NEW.captain_id IS NOT OLD.captain_id
BEGIN
    SELECT RAISE(ABORT, 'MB002:teams_captain_is_member: the captain must be a member of the team')
     WHERE NOT EXISTS (SELECT 1 FROM team_members WHERE team_id = NEW.id AND user_id = NEW.captain_id);
END;

CREATE TRIGGER IF NOT EXISTS solo_players_not_in_team BEFORE INSERT ON solo_players
BEGIN
    SELECT RAISE(ABORT, 'MB001:solo_team_conflict: a team member cannot register solo')
     WHERE EXISTS (
        SELECT 1 FROM team_members m JOIN teams t ON t.id = m.team_id
         WHERE m.user_id = NEW.user_id
           AND t.tournament_id IS COALESCE(NEW.tournament_id, (SELECT id FROM tournaments WHERE status = 'active' LIMIT 1)));
END;

-- ---------------------------------------------------------------- formats
CREATE TRIGGER IF NOT EXISTS teams_tokens_insert BEFORE INSERT ON teams
BEGIN
    SELECT RAISE(ABORT, '23514:teams_invite_token_format: invite tokens are 32-48 URL-safe characters')
     WHERE (NEW.invite_token IS NOT NULL AND (length(NEW.invite_token) NOT BETWEEN 32 AND 48 OR NEW.invite_token GLOB '*[^A-Za-z0-9_-]*'))
        OR (NEW.captain_invite_token IS NOT NULL AND (length(NEW.captain_invite_token) NOT BETWEEN 32 AND 48 OR NEW.captain_invite_token GLOB '*[^A-Za-z0-9_-]*'));
    SELECT RAISE(ABORT, '23514:teams_tokens_differ: the captain token must differ from the player token')
     WHERE NEW.captain_invite_token = NEW.invite_token;
END;

CREATE TRIGGER IF NOT EXISTS teams_tokens_update BEFORE UPDATE OF invite_token, captain_invite_token ON teams
BEGIN
    SELECT RAISE(ABORT, '23514:teams_invite_token_format: invite tokens are 32-48 URL-safe characters')
     WHERE (NEW.invite_token IS NOT NULL AND (length(NEW.invite_token) NOT BETWEEN 32 AND 48 OR NEW.invite_token GLOB '*[^A-Za-z0-9_-]*'))
        OR (NEW.captain_invite_token IS NOT NULL AND (length(NEW.captain_invite_token) NOT BETWEEN 32 AND 48 OR NEW.captain_invite_token GLOB '*[^A-Za-z0-9_-]*'));
    SELECT RAISE(ABORT, '23514:teams_tokens_differ: the captain token must differ from the player token')
     WHERE NEW.captain_invite_token = NEW.invite_token;
END;

-- same rule as app.utils.text.USERNAME_RE: ^[a-z][a-z0-9_]{4,31}$
CREATE TRIGGER IF NOT EXISTS users_username_insert BEFORE INSERT ON users
WHEN NEW.username_norm IS NOT NULL
 AND (length(NEW.username_norm) NOT BETWEEN 5 AND 32 OR NEW.username_norm NOT GLOB '[a-z]*' OR NEW.username_norm GLOB '*[^a-z0-9_]*')
BEGIN
    SELECT RAISE(ABORT, '23514:users_username_norm_format: malformed username');
END;

CREATE TRIGGER IF NOT EXISTS users_username_update BEFORE UPDATE OF username_norm ON users
WHEN NEW.username_norm IS NOT NULL
 AND (length(NEW.username_norm) NOT BETWEEN 5 AND 32 OR NEW.username_norm NOT GLOB '[a-z]*' OR NEW.username_norm GLOB '*[^a-z0-9_]*')
BEGIN
    SELECT RAISE(ABORT, '23514:users_username_norm_format: malformed username');
END;

-- role is one role or a comma list of them (solo multi-roles); every replace runs twice so adjacent
-- duplicates (',EXP,EXP,') are consumed too
CREATE TRIGGER IF NOT EXISTS solo_players_role_insert BEFORE INSERT ON solo_players
WHEN NEW.role IS NOT NULL AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
        ',' || NEW.role || ',',
        ',EXP,', ',,'), ',EXP,', ',,'), ',GOLD,', ',,'), ',GOLD,', ',,'), ',MID,', ',,'), ',MID,', ',,'),
        ',JUNGLE,', ',,'), ',JUNGLE,', ',,'), ',ROAM,', ',,'), ',ROAM,', ',,'), ',ALL,', ',,'), ',ALL,', ',,')
     GLOB '*[^,]*'
BEGIN
    SELECT RAISE(ABORT, '23514:solo_players_role_check: unknown role');
END;

CREATE TRIGGER IF NOT EXISTS solo_players_role_update BEFORE UPDATE OF role ON solo_players
WHEN NEW.role IS NOT NULL AND replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
        ',' || NEW.role || ',',
        ',EXP,', ',,'), ',EXP,', ',,'), ',GOLD,', ',,'), ',GOLD,', ',,'), ',MID,', ',,'), ',MID,', ',,'),
        ',JUNGLE,', ',,'), ',JUNGLE,', ',,'), ',ROAM,', ',,'), ',ROAM,', ',,'), ',ALL,', ',,'), ',ALL,', ',,')
     GLOB '*[^,]*'
BEGIN
    SELECT RAISE(ABORT, '23514:solo_players_role_check: unknown role');
END;

-- ---------------------------------------------------------------- matches
CREATE TRIGGER IF NOT EXISTS matches_check_insert BEFORE INSERT ON matches
BEGIN
    SELECT RAISE(ABORT, '23514:matches_distinct_teams: a team cannot play itself')
     WHERE NEW.team_a_id IS NOT NULL AND NEW.team_a_id = NEW.team_b_id;
    SELECT RAISE(ABORT, '23514:matches_winner_participant: the winner must be one of the two teams')
     WHERE NEW.winner_id IS NOT NULL AND NEW.winner_id IS NOT NEW.team_a_id AND NEW.winner_id IS NOT NEW.team_b_id;
    SELECT RAISE(ABORT, '23514:matches_winner_iff_finished: a finished match has a winner, an unfinished one has none')
     WHERE (NEW.status = 'finished') <> (NEW.winner_id IS NOT NULL);
END;

CREATE TRIGGER IF NOT EXISTS matches_check_update BEFORE UPDATE OF team_a_id, team_b_id, winner_id, status ON matches
BEGIN
    SELECT RAISE(ABORT, '23514:matches_distinct_teams: a team cannot play itself')
     WHERE NEW.team_a_id IS NOT NULL AND NEW.team_a_id = NEW.team_b_id;
    SELECT RAISE(ABORT, '23514:matches_winner_participant: the winner must be one of the two teams')
     WHERE NEW.winner_id IS NOT NULL AND NEW.winner_id IS NOT NEW.team_a_id AND NEW.winner_id IS NOT NEW.team_b_id;
    SELECT RAISE(ABORT, '23514:matches_winner_iff_finished: a finished match has a winner, an unfinished one has none')
     WHERE (NEW.status = 'finished') <> (NEW.winner_id IS NOT NULL);
END;

-- ---------------------------------------------------------------- broadcasts
CREATE TRIGGER IF NOT EXISTS broadcasts_check_insert BEFORE INSERT ON broadcasts
BEGIN
    SELECT RAISE(ABORT, '23514:broadcasts_audience_check: unknown audience')
     WHERE NEW.audience NOT IN ('captains', 'solo', 'all');
    SELECT RAISE(ABORT, '23514:broadcasts_content_check: text needs text, media needs a file')
     WHERE NEW.content_type NOT IN ('text', 'photo', 'video', 'document')
        OR (NEW.content_type = 'text' AND (NEW.text IS NULL OR NEW.text = ''))
        OR (NEW.content_type <> 'text' AND NEW.file_id IS NULL);
END;

-- ---------------------------------------------------------------- deputies: at most two active
CREATE TRIGGER IF NOT EXISTS deputies_limit_insert BEFORE INSERT ON deputies
BEGIN
    SELECT RAISE(ABORT, 'MB008:deputies_max2: at most two active deputies')
     WHERE (SELECT count(*) FROM deputies WHERE revoked_at IS NULL AND user_id <> NEW.user_id) >= 2;
END;

CREATE TRIGGER IF NOT EXISTS deputies_limit_reactivate BEFORE UPDATE OF revoked_at ON deputies
WHEN NEW.revoked_at IS NULL AND OLD.revoked_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'MB008:deputies_max2: at most two active deputies')
     WHERE (SELECT count(*) FROM deputies WHERE revoked_at IS NULL AND user_id <> NEW.user_id) >= 2;
END;

-- ---------------------------------------------------------------- indexes on foreign-key columns
-- (lookups such as "the team this user registered" and every parent-row delete scan these columns)
CREATE INDEX IF NOT EXISTS teams_created_by_idx ON teams (created_by);
CREATE INDEX IF NOT EXISTS matches_team_a_idx ON matches (team_a_id);
CREATE INDEX IF NOT EXISTS matches_team_b_idx ON matches (team_b_id);
CREATE INDEX IF NOT EXISTS matches_winner_idx ON matches (winner_id);
CREATE INDEX IF NOT EXISTS matches_next_match_idx ON matches (next_match_id);
CREATE INDEX IF NOT EXISTS match_games_winner_idx ON match_games (winner_id);
CREATE INDEX IF NOT EXISTS solo_players_tournament_idx ON solo_players (tournament_id);
CREATE INDEX IF NOT EXISTS tournaments_completed_by_idx ON tournaments (completed_by);
CREATE INDEX IF NOT EXISTS deputies_approved_by_idx ON deputies (approved_by);
CREATE INDEX IF NOT EXISTS deputies_revoked_by_idx ON deputies (revoked_by);
CREATE INDEX IF NOT EXISTS deputies_invite_idx ON deputies (invite_id);
CREATE INDEX IF NOT EXISTS deputy_invites_created_by_idx ON deputy_invites (created_by);
CREATE INDEX IF NOT EXISTS deputy_invites_candidate_idx ON deputy_invites (candidate_id);
CREATE INDEX IF NOT EXISTS deputy_invites_decided_by_idx ON deputy_invites (decided_by);
CREATE INDEX IF NOT EXISTS result_broadcasts_created_by_idx ON result_broadcasts (created_by);
CREATE INDEX IF NOT EXISTS reviews_user_idx ON reviews (user_id);
