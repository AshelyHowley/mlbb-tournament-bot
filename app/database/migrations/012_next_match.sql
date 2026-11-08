-- Add next_match_id to link matches for winner advancement
ALTER TABLE matches ADD COLUMN next_match_id INTEGER REFERENCES matches(id);
