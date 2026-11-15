-- Матч за 3-е место и раунд 1/32.
-- 009 пытался добавить r32 с sort_order 5, но это место уже занял playin (sort_order UNIQUE),
-- поэтому r32 молча не добавлялся и сетка на 33-64 команды падала по внешнему ключу.
INSERT OR IGNORE INTO stages (code, title, sort_order) VALUES ('r32', '1/32', 3);
INSERT OR IGNORE INTO stages (code, title, sort_order) VALUES ('third', '3 место', 45);
-- «коробка чемпиона» сетки (bracket_builder.save_plan) - в рабочей базе её добавили вручную,
-- на чистой установке её не было и генерация сетки падала по внешнему ключу
INSERT OR IGNORE INTO stages (code, title, sort_order) VALUES ('finalist', 'Финалист', 60);

-- Один финал, один матч за 3-е место и одна «коробка чемпиона» на турнир.
CREATE UNIQUE INDEX IF NOT EXISTS matches_one_per_stage_uq ON matches (tournament_id, stage_code)
    WHERE stage_code IN ('final', 'third', 'finalist');

-- Записи состава, чьих команд уже нет (остались от скриптов, работавших без PRAGMA foreign_keys).
DELETE FROM team_members WHERE team_id NOT IN (SELECT id FROM teams);
