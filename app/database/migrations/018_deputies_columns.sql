-- The repositories record who approved / revoked a deputy and through which invite,
-- but 001_sqlite created deputies without these columns, so approving a deputy failed.
ALTER TABLE deputies ADD COLUMN invite_id INTEGER REFERENCES deputy_invites (id);
ALTER TABLE deputies ADD COLUMN approved_by INTEGER REFERENCES users (telegram_id);
ALTER TABLE deputies ADD COLUMN revoked_by INTEGER REFERENCES users (telegram_id);
