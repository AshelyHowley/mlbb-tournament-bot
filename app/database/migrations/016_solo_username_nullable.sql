-- Telegram accounts may have no username: solo_players.username / username_norm must be nullable
-- (001_sqlite declared them NOT NULL, so solo registration failed for such players).
-- SQLite cannot drop NOT NULL in place, so the table is rebuilt with all data kept.
CREATE TABLE solo_players_new (
    user_id           INTEGER PRIMARY KEY REFERENCES users (telegram_id),
    username          TEXT,
    username_norm     TEXT,
    role              TEXT,
    tournament_id     INTEGER REFERENCES tournaments (id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    display_full_name TEXT,
    stars             INTEGER DEFAULT 0
);
INSERT INTO solo_players_new (user_id, username, username_norm, role, tournament_id, created_at, display_full_name, stars)
    SELECT user_id, username, username_norm, role, tournament_id, created_at, display_full_name, stars FROM solo_players;
DROP TABLE solo_players;
ALTER TABLE solo_players_new RENAME TO solo_players;
