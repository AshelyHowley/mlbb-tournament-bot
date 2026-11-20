-- Teams assembled by the admin from solo players ("Собрать команды из соло").
-- Their players KEEP their solo registration, so the admin can disband and re-assemble the teams later.
-- The solo/team exclusivity triggers (019) therefore skip teams flagged from_solo = 1.
ALTER TABLE teams ADD COLUMN from_solo INTEGER NOT NULL DEFAULT 0;

DROP TRIGGER IF EXISTS team_members_guard_insert;
CREATE TRIGGER team_members_guard_insert BEFORE INSERT ON team_members
BEGIN
    SELECT RAISE(ABORT, 'MB003:team_members_max5: a team has at most 5 players')
     WHERE (SELECT count(*) FROM team_members WHERE team_id = NEW.team_id) >= 5;
    SELECT RAISE(ABORT, '23505:team_members_user_uq: the user already plays for another team of this tournament')
     WHERE EXISTS (
        SELECT 1 FROM team_members m JOIN teams t ON t.id = m.team_id
         WHERE m.user_id = NEW.user_id AND m.team_id <> NEW.team_id
           AND t.tournament_id IS (SELECT tournament_id FROM teams WHERE id = NEW.team_id));
    SELECT RAISE(ABORT, 'MB001:solo_team_conflict: a solo player cannot join a team')
     WHERE (SELECT from_solo FROM teams WHERE id = NEW.team_id) = 0
       AND EXISTS (
        SELECT 1 FROM solo_players s
         WHERE s.user_id = NEW.user_id
           AND s.tournament_id IS (SELECT tournament_id FROM teams WHERE id = NEW.team_id));
    SELECT RAISE(ABORT, 'MB002:team_members_captain_flag: the captain flag must match teams.captain_id')
     WHERE NEW.is_captain = 1 AND (SELECT captain_id FROM teams WHERE id = NEW.team_id) IS NOT NEW.user_id;
END;

DROP TRIGGER IF EXISTS solo_players_not_in_team;
CREATE TRIGGER solo_players_not_in_team BEFORE INSERT ON solo_players
BEGIN
    SELECT RAISE(ABORT, 'MB001:solo_team_conflict: a team member cannot register solo')
     WHERE EXISTS (
        SELECT 1 FROM team_members m JOIN teams t ON t.id = m.team_id
         WHERE m.user_id = NEW.user_id AND t.from_solo = 0
           AND t.tournament_id IS COALESCE(NEW.tournament_id, (SELECT id FROM tournaments WHERE status = 'active' LIMIT 1)));
END;
