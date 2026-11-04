-- SQLite schema for MLBB Tournament Bot (converted from Postgres migrations)
-- Simplified: no plpgsql triggers. Business rules enforced in Python.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    username_norm TEXT,
    full_name     TEXT NOT NULL DEFAULT '',
    is_blocked    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS users_username_norm_idx ON users (username_norm);

CREATE TABLE IF NOT EXISTS tournaments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','closed_early')),
    started_at           TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at         TEXT,
    completed_by         INTEGER REFERENCES users (telegram_id),
    first_place_team_id  INTEGER,
    second_place_team_id INTEGER,
    third_place_team_id  INTEGER,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS tournaments_one_active_uq ON tournaments (status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS teams (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    name_norm             TEXT NOT NULL,
    captain_id            INTEGER REFERENCES users (telegram_id),
    captain_username_norm TEXT,
    created_by            INTEGER REFERENCES users (telegram_id),
    invite_token          TEXT,
    captain_invite_token  TEXT,
    tournament_id         INTEGER REFERENCES tournaments (id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name_norm, tournament_id)
);
CREATE INDEX IF NOT EXISTS teams_captain_idx ON teams (captain_id);
CREATE INDEX IF NOT EXISTS teams_tournament_idx ON teams (tournament_id);

CREATE TABLE IF NOT EXISTS team_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    INTEGER NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users (telegram_id),
    is_captain INTEGER NOT NULL DEFAULT 0,
    position   INTEGER,
    username   TEXT,
    username_norm TEXT,
    UNIQUE (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS team_members_user_idx ON team_members (user_id);

CREATE TABLE IF NOT EXISTS solo_players (
    user_id       INTEGER PRIMARY KEY REFERENCES users (telegram_id),
    username      TEXT NOT NULL,
    username_norm TEXT NOT NULL,
    role          TEXT,
    tournament_id INTEGER REFERENCES tournaments (id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stages (
    code       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    sort_order INTEGER NOT NULL UNIQUE
);
INSERT OR IGNORE INTO stages (code, title, sort_order) VALUES
    ('r16',   '1/16',      10),
    ('r8',    '1/8',       20),
    ('r4',    '1/4',       30),
    ('semi',  'Полуфинал', 40),
    ('final', 'Финал',     50);

CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_code    TEXT NOT NULL REFERENCES stages (code),
    team_a_id     INTEGER NOT NULL REFERENCES teams (id),
    team_b_id     INTEGER NOT NULL REFERENCES teams (id),
    winner_id     INTEGER REFERENCES teams (id),
    status        TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','finished')),
    tournament_id INTEGER REFERENCES tournaments (id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    CHECK (team_a_id <> team_b_id)
);
CREATE INDEX IF NOT EXISTS matches_stage_idx ON matches (stage_code);
CREATE INDEX IF NOT EXISTS matches_tournament_idx ON matches (tournament_id);

CREATE TABLE IF NOT EXISTS match_games (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id  INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    game_no   INTEGER NOT NULL,
    winner_id INTEGER REFERENCES teams (id),
    UNIQUE (match_id, game_no)
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id     INTEGER NOT NULL,
    audience     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    text         TEXT,
    file_id      TEXT,
    status       TEXT NOT NULL DEFAULT 'draft',
    total        INTEGER NOT NULL DEFAULT 0,
    sent         INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    blocked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS deputies (
    user_id     INTEGER PRIMARY KEY REFERENCES users (telegram_id),
    approved_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings (key, value) VALUES ('registration_open', 'true');

-- Ensure one active tournament exists
INSERT INTO tournaments (status, started_at)
SELECT 'active', datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM tournaments WHERE status = 'active');
