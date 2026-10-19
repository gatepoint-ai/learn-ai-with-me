-- ============================================================================
-- knowledge -- RAG vector store (pgvector on TimescaleDB)
-- ============================================================================
-- The dimension is fixed at 384 to match the default embedding model
-- (BAAI/bge-small-en-v1.5). Changing model means changing dimension, which is a
-- new migration AND a re-ingest -- the collection table records what produced
-- its vectors so the ingester can refuse a mismatch loudly.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- pgvectorscale ships with the timescaledb-ha image and provides the
-- StreamingDiskANN index. It is optional: if unavailable we fall back to HNSW
-- below, so a plain pgvector image still works.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'vectorscale unavailable, using HNSW instead';
END
$$;

CREATE TABLE IF NOT EXISTS collection (
    name            TEXT PRIMARY KEY,
    description     TEXT NOT NULL DEFAULT '',
    -- These two are the guard against silently mixing vector spaces. The
    -- ingester compares them to its live config and refuses on mismatch.
    embedding_model TEXT NOT NULL,
    embedding_dim   INTEGER NOT NULL,
    chunk_size      INTEGER NOT NULL,
    chunk_overlap   INTEGER NOT NULL,
    document_count  INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    ingested_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document (
    id              BIGSERIAL PRIMARY KEY,
    collection      TEXT NOT NULL REFERENCES collection(name) ON DELETE CASCADE,
    -- Path relative to the docs root, so it is stable across containers.
    source_path     TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    -- sha256 of the file body. Re-ingesting an unchanged file is a no-op.
    content_hash    TEXT NOT NULL,
    byte_size       INTEGER NOT NULL DEFAULT 0,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection, source_path)
);

CREATE TABLE IF NOT EXISTS chunk (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    collection      TEXT NOT NULL REFERENCES collection(name) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    -- Kept alongside the embedding so a retrieval hit can cite an exact
    -- location, not just a filename.
    heading         TEXT NOT NULL DEFAULT '',
    token_estimate  INTEGER NOT NULL DEFAULT 0,
    embedding       vector(384) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

-- Filtering by collection before the vector scan is the common access path.
CREATE INDEX IF NOT EXISTS chunk_collection_idx ON chunk (collection);

-- Vector index. StreamingDiskANN when vectorscale loaded, HNSW otherwise.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vectorscale') THEN
        CREATE INDEX IF NOT EXISTS chunk_embedding_diskann
            ON chunk USING diskann (embedding vector_cosine_ops);
    ELSE
        CREATE INDEX IF NOT EXISTS chunk_embedding_hnsw
            ON chunk USING hnsw (embedding vector_cosine_ops);
    END IF;
END
$$;
