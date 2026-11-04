-- Add display columns used by the bot

ALTER TABLE solo_players ADD COLUMN display_full_name TEXT;
ALTER TABLE team_members ADD COLUMN display_username TEXT;
ALTER TABLE team_members ADD COLUMN display_full_name TEXT;
