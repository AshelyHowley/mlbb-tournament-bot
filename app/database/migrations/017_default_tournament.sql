-- A row inserted without tournament_id belongs to the ACTIVE tournament (as in the PostgreSQL schema).
-- The repositories always pass it explicitly; this only guards raw / legacy inserts.
CREATE TRIGGER IF NOT EXISTS teams_default_tournament AFTER INSERT ON teams
WHEN NEW.tournament_id IS NULL
BEGIN
    UPDATE teams SET tournament_id = (SELECT id FROM tournaments WHERE status = 'active' LIMIT 1) WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS solo_default_tournament AFTER INSERT ON solo_players
WHEN NEW.tournament_id IS NULL
BEGIN
    UPDATE solo_players SET tournament_id = (SELECT id FROM tournaments WHERE status = 'active' LIMIT 1) WHERE user_id = NEW.user_id;
END;
