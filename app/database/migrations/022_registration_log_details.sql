-- История регистраций: полный снимок участника на момент события (а не только id и роль).
ALTER TABLE registration_log ADD COLUMN username TEXT;
ALTER TABLE registration_log ADD COLUMN full_name TEXT;
ALTER TABLE registration_log ADD COLUMN team_id INTEGER;
ALTER TABLE registration_log ADD COLUMN member_role TEXT;
ALTER TABLE registration_log ADD COLUMN roles TEXT;
ALTER TABLE registration_log ADD COLUMN stars INTEGER;
ALTER TABLE registration_log ADD COLUMN position TEXT;
ALTER TABLE registration_log ADD COLUMN tournament_id INTEGER;
