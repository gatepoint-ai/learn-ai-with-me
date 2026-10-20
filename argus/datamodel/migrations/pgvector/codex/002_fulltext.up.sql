-- ============================================================================
-- Full-text search alongside the vectors -- the keyword half of hybrid search.
--
-- Embeddings are good at meaning and bad at exact tokens: "CTL-REV-004" and
-- "CTL-REV-003" sit almost on top of each other in vector space. A keyword
-- index matches the token itself. The 'simple' configuration keeps IDs and
-- codes intact instead of stemming them as English words.
-- ============================================================================

ALTER TABLE chunk
    ADD COLUMN IF NOT EXISTS search_text tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(heading, '')), 'A') ||
        setweight(to_tsvector('simple', content), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS chunk_search_text_idx ON chunk USING gin (search_text);
