-- Who already got a general broadcast, so an interrupted one can be resumed without duplicates.
-- tracked = 1: the broadcast logs every recipient (only those can be resumed; older ones have no log).
-- notified = 1: the sender has already been told it was interrupted (and offered to resume).
ALTER TABLE broadcasts ADD COLUMN tracked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE broadcasts ADD COLUMN notified INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts (id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL,
    outcome      TEXT NOT NULL,
    PRIMARY KEY (broadcast_id, recipient_id)
);
