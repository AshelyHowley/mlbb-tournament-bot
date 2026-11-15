-- Add individual rank for team members
ALTER TABLE team_members ADD COLUMN rank INTEGER DEFAULT 0;
