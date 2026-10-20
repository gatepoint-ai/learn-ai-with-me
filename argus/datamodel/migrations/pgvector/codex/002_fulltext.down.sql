DROP INDEX IF EXISTS chunk_search_text_idx;
ALTER TABLE chunk DROP COLUMN IF EXISTS search_text;
