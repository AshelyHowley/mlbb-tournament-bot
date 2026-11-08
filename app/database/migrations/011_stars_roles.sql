-- Add stars column to solo_players and teams
ALTER TABLE solo_players ADD COLUMN stars INTEGER DEFAULT 0;
ALTER TABLE teams ADD COLUMN stars INTEGER DEFAULT 0;
-- roles already exists as TEXT, we'll store comma-separated for multi-role
