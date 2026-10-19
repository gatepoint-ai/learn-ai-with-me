-- ============================================================================
-- agent -- conversation history and tool-call audit
-- ============================================================================

CREATE TABLE IF NOT EXISTS conversation (
    id          UUID PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS message (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    -- Which agent path produced this: 'native' tool loop or 'constrained'
    -- query-plan mode. Surfaced in the UI so a weak-model answer is labelled.
    agent_mode      TEXT,
    tool_calls      JSONB NOT NULL DEFAULT '[]'::jsonb,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS message_conversation_idx
    ON message (conversation_id, created_at);

-- Every tool invocation, recorded whether or not it succeeded.
--
-- This is the audit trail that makes the agent debuggable: when an answer is
-- wrong, the question is almost always which tool it called and what came back.
-- It is also what the routing smoke test asserts against.
CREATE TABLE IF NOT EXISTS tool_call (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    tool_name       TEXT NOT NULL,
    arguments       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Truncated result preview; the full payload is not retained.
    result_preview  TEXT NOT NULL DEFAULT '',
    row_count       INTEGER,
    duration_ms     INTEGER,
    is_error        BOOLEAN NOT NULL DEFAULT FALSE,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tool_call_conversation_idx
    ON tool_call (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS tool_call_name_idx ON tool_call (tool_name, created_at DESC);
