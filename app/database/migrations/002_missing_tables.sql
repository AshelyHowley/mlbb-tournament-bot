-- Missing tables for SQLite (result broadcasts, deputy invites)

CREATE TABLE IF NOT EXISTS deputy_invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    created_by   INTEGER NOT NULL REFERENCES users (telegram_id),
    status       TEXT NOT NULL DEFAULT 'open',
    candidate_id INTEGER REFERENCES users (telegram_id),
    claimed_at   TEXT,
    decided_at   TEXT,
    decided_by   INTEGER REFERENCES users (telegram_id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS result_broadcasts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    context_id   INTEGER NOT NULL,
    created_by   INTEGER NOT NULL REFERENCES users (telegram_id),
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

CREATE TABLE IF NOT EXISTS result_broadcast_deliveries (
    result_broadcast_id INTEGER NOT NULL REFERENCES result_broadcasts (id) ON DELETE CASCADE,
    recipient_id         INTEGER NOT NULL,
    status                TEXT NOT NULL,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (result_broadcast_id, recipient_id)
);

-- Also ensure deputies has the extra columns if missing
-- (SQLite doesn't support ADD COLUMN IF NOT EXISTS easily; skip if already present)
