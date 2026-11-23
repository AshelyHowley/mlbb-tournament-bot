-- General broadcasts can be an album (2-10 photos/videos, or 2-10 documents).
-- media: JSON list of [type, file_id]; file_id keeps the first file so the content check stays meaningful.
ALTER TABLE broadcasts ADD COLUMN media TEXT;

DROP TRIGGER IF EXISTS broadcasts_check_insert;
CREATE TRIGGER broadcasts_check_insert BEFORE INSERT ON broadcasts
BEGIN
    SELECT RAISE(ABORT, '23514:broadcasts_audience_check: unknown audience')
     WHERE NEW.audience NOT IN ('captains', 'solo', 'all');
    SELECT RAISE(ABORT, '23514:broadcasts_content_check: text needs text, media needs a file')
     WHERE NEW.content_type NOT IN ('text', 'photo', 'video', 'document', 'album')
        OR (NEW.content_type = 'text' AND (NEW.text IS NULL OR NEW.text = ''))
        OR (NEW.content_type <> 'text' AND NEW.file_id IS NULL)
        OR (NEW.content_type = 'album' AND NEW.media IS NULL);
END;
