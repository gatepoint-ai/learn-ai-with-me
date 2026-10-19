-- ============================================================================
-- kpi -- computed KPI results
-- ============================================================================
-- The engine recomputes a KPI by deleting its rows for the affected window and
-- re-inserting. Every table therefore has a natural key with a unique index, so
-- a partial re-run cannot leave duplicate points behind.
-- ============================================================================

CREATE TABLE IF NOT EXISTS kpi_definition (
    kpi_id          TEXT PRIMARY KEY,
    name            TEXT        NOT NULL,
    description     TEXT        NOT NULL DEFAULT '',
    lens          TEXT        NOT NULL,
    unit            TEXT        NOT NULL DEFAULT 'count',
    grain           TEXT        NOT NULL DEFAULT 'day',
    direction       TEXT        NOT NULL DEFAULT 'neutral',
    tags            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- The spec as loaded, kept so the API can serve thresholds and breakdown
    -- lists without re-reading the config directory.
    spec            JSONB       NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The headline series: one value per KPI per period.
CREATE TABLE IF NOT EXISTS kpi_value (
    kpi_id          TEXT        NOT NULL REFERENCES kpi_definition(kpi_id) ON DELETE CASCADE,
    period          DATE        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    -- Populated only for ratio measures, so a ratio can be re-derived or
    -- re-weighted without going back to ClickHouse.
    numerator       DOUBLE PRECISION,
    denominator     DOUBLE PRECISION,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kpi_id, period)
);

CREATE INDEX IF NOT EXISTS kpi_value_period_idx ON kpi_value (period DESC);

-- Per-dimension decomposition of the same measure, one row per dimension value.
CREATE TABLE IF NOT EXISTS kpi_breakdown (
    kpi_id          TEXT        NOT NULL REFERENCES kpi_definition(kpi_id) ON DELETE CASCADE,
    period          DATE        NOT NULL,
    dimension       TEXT        NOT NULL,
    dimension_value TEXT        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kpi_id, period, dimension, dimension_value)
);

CREATE INDEX IF NOT EXISTS kpi_breakdown_lookup_idx
    ON kpi_breakdown (kpi_id, dimension, period DESC);

-- Forward projection. `period` is in the future relative to the last actual.
CREATE TABLE IF NOT EXISTS kpi_forecast (
    kpi_id           TEXT       NOT NULL REFERENCES kpi_definition(kpi_id) ON DELETE CASCADE,
    period           DATE       NOT NULL,
    forecast_value   DOUBLE PRECISION NOT NULL,
    confidence_lower DOUBLE PRECISION NOT NULL,
    confidence_upper DOUBLE PRECISION NOT NULL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kpi_id, period)
);

-- Points the detector flagged, with the baseline it was judged against so the
-- verdict can be explained after the fact.
CREATE TABLE IF NOT EXISTS kpi_anomaly (
    id              BIGSERIAL   PRIMARY KEY,
    kpi_id          TEXT        NOT NULL REFERENCES kpi_definition(kpi_id) ON DELETE CASCADE,
    period          DATE        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    expected        DOUBLE PRECISION NOT NULL,
    deviation       DOUBLE PRECISION NOT NULL,
    z_score         DOUBLE PRECISION NOT NULL,
    severity        TEXT        NOT NULL DEFAULT 'medium',
    reason          TEXT        NOT NULL DEFAULT '',
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kpi_id, period)
);

CREATE INDEX IF NOT EXISTS kpi_anomaly_recent_idx ON kpi_anomaly (period DESC);

-- AI-generated narrative for a KPI over a window. Nullable model column so a
-- row written with no LLM configured is still valid.
CREATE TABLE IF NOT EXISTS kpi_insight (
    id              BIGSERIAL   PRIMARY KEY,
    kpi_id          TEXT        NOT NULL REFERENCES kpi_definition(kpi_id) ON DELETE CASCADE,
    period_start    DATE        NOT NULL,
    period_end      DATE        NOT NULL,
    headline        TEXT        NOT NULL,
    narrative       TEXT        NOT NULL,
    model           TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kpi_insight_lookup_idx
    ON kpi_insight (kpi_id, generated_at DESC);

-- Bookkeeping for recalculation runs, so a stalled run is visible.
CREATE TABLE IF NOT EXISTS kpi_run (
    id              BIGSERIAL   PRIMARY KEY,
    kpi_id          TEXT,
    status          TEXT        NOT NULL DEFAULT 'running',
    points_written  INTEGER     NOT NULL DEFAULT 0,
    anomalies_found INTEGER     NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
