-- Make team_b_id nullable so empty bracket slots can exist
-- SQLite cannot ALTER COLUMN, so we recreate the table

CREATE TABLE matches_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments (id),
    stage_code    TEXT NOT NULL REFERENCES stages (code),
    team_a_id     INTEGER REFERENCES teams (id),
    team_b_id     INTEGER REFERENCES teams (id),
    winner_id     INTEGER REFERENCES teams (id),
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

INSERT INTO matches_new (id, tournament_id, stage_code, team_a_id, team_b_id, winner_id, status, created_at, finished_at)
SELECT id, tournament_id, stage_code, team_a_id, team_b_id, winner_id, status, created_at, finished_at FROM matches;

DROP TABLE matches;
ALTER TABLE matches_new RENAME TO matches;

CREATE INDEX IF NOT EXISTS matches_tournament_idx ON matches (tournament_id);
CREATE INDEX IF NOT EXISTS matches_stage_idx ON matches (stage_code);
