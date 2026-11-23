-- notified = 1: the author was already told this result notification was interrupted (and offered to resume).
-- Notifications interrupted before this update count as already told, so nobody gets a pile of old notices.
ALTER TABLE result_broadcasts ADD COLUMN notified INTEGER NOT NULL DEFAULT 0;
UPDATE result_broadcasts SET notified = 1 WHERE status IN ('interrupted', 'sending');
