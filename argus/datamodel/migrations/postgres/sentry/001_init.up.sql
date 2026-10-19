-- ============================================================================
-- sentry -- rules and alerts
-- ============================================================================
-- Rules and probes are mirrored from YAML on every sentry start,
-- so this schema is a projection of config rather than a source of truth. The
-- alerts are the real data.
-- ============================================================================

CREATE TABLE IF NOT EXISTS subject_type (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alert_type (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rule (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    process     TEXT NOT NULL DEFAULT '',
    owner       TEXT NOT NULL DEFAULT '',
    ruleset   TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS probe (
    id                  TEXT PRIMARY KEY,
    rule_id          TEXT NOT NULL REFERENCES rule(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    lens              TEXT NOT NULL,
    comparator          TEXT NOT NULL,
    threshold           DOUBLE PRECISION NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'medium',
    alert_type  INTEGER NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    -- Full spec kept so the API can explain an alert without loading config.
    spec                JSONB NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Alerts.
--
-- The unique constraint on (probe_id, subject_id, period) is what makes
-- re-running validation idempotent: a second run over the same day updates the
-- existing alert instead of stacking a duplicate. Without it, `make bootstrap`
-- run twice would double every count on the dashboard.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alert (
    id                  BIGSERIAL PRIMARY KEY,
    probe_id    TEXT NOT NULL REFERENCES probe(id) ON DELETE CASCADE,
    subject_type         TEXT NOT NULL,
    subject_id           TEXT NOT NULL,
    period              DATE NOT NULL,
    alert_type  INTEGER NOT NULL,
    severity            TEXT NOT NULL,
    observed_value      DOUBLE PRECISION NOT NULL,
    baseline_value      DOUBLE PRECISION,
    threshold           DOUBLE PRECISION NOT NULL,
    deviation           DOUBLE PRECISION,
    summary             TEXT NOT NULL,
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'open',
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (probe_id, subject_id, period)
);

CREATE INDEX IF NOT EXISTS alert_period_idx   ON alert (period DESC);
CREATE INDEX IF NOT EXISTS alert_subject_idx   ON alert (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS alert_severity_idx ON alert (severity, period DESC);
CREATE INDEX IF NOT EXISTS alert_status_idx   ON alert (status) WHERE status = 'open';

-- One row per sentry run, so a run that found nothing is distinguishable
-- from a run that never happened. The smoke test relies on that distinction.
CREATE TABLE IF NOT EXISTS sentry_run (
    id                  BIGSERIAL PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'running',
    probes_evaluated    INTEGER NOT NULL DEFAULT 0,
    rows_examined       INTEGER NOT NULL DEFAULT 0,
    alerts_written    INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
