-- ============================================================================
-- analytics -- AI Revenue Assurance analytics schema (ClickHouse)
-- ============================================================================
--
-- LAYERING
-- ========
--   analytics.usage_event                     raw fact, one row per usage event
--     +-- MV1 --> analytics.usage_daily_agg          full dimensional grain
--     +-- MV2 --> analytics.leakage_daily_agg        site x day rollup
--
-- The aggregates are SummingMergeTree, so numeric columns merge additively.
-- A re-inserted event therefore ADDS to its dimension row rather than replacing
-- it, which is why the seeder truncates instead of upserting.
--
-- SIZING (10 sites x 4 channels x 3 classes x 4 statuses x 2 home/away)
--   usage_event      ~40k rows/day seeded  ->  ~1.2M rows at 30 days
--   usage_daily_agg  ~960 combos/day       ->  ~29k rows at 30 days
-- Both are trivial at demo scale. The layering exists to mirror how this
-- behaves at production volume, where only the aggregates are ever queried.
--
-- GRAIN RULE
-- ==========
-- Prism lenses read the _agg tables, never usage_event. Keeping that rule
-- means a lens query scans a few thousand rows regardless of how many raw
-- events sit behind it.
-- ============================================================================

CREATE DATABASE IF NOT EXISTS analytics;

-- ----------------------------------------------------------------------------
-- Dimension lookups. Small, ReplacingMergeTree so re-seeding is idempotent.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analytics.site
(
    site_id     LowCardinality(String),
    site_name   String,
    region      LowCardinality(String),
    updated_at  DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY site_id;

CREATE TABLE IF NOT EXISTS analytics.partner
(
    partner_id      LowCardinality(String),
    partner_name    String,
    is_home_partner UInt8,
    updated_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY partner_id;

CREATE TABLE IF NOT EXISTS analytics.product
(
    product_id   LowCardinality(String),
    product_name String,
    unit_price   Decimal(18, 4),
    updated_at   DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY product_id;

-- ----------------------------------------------------------------------------
-- Fact table.
--
-- The three is_* flags are the leakage attribution: an event that was never
-- billed, was billed at the wrong rate, or could not be matched to an account.
-- Storing them as UInt8 lets the aggregates sum them straight into counts.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analytics.usage_event
(
    event_id            UUID,
    event_ts            DateTime,
    site_id             LowCardinality(String),
    partner_id          LowCardinality(String),
    account_id          String,
    product_id          LowCardinality(String),
    is_home_partner     UInt8,

    channel             LowCardinality(String),
    customer_class      LowCardinality(String),
    collection_status   LowCardinality(String),

    amount_expected     Decimal(18, 2),
    amount_billed       Decimal(18, 2),
    amount_collected    Decimal(18, 2),
    amount_discount     Decimal(18, 2),

    is_billed           UInt8,
    is_rated_correctly  UInt8,
    is_matched          UInt8,

    ingested_at         DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_ts)
ORDER BY (event_ts, site_id, channel, collection_status)
SETTINGS index_granularity = 8192;

-- ----------------------------------------------------------------------------
-- LAYER 1: full dimensional grain. KPI breakdowns read this.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analytics.usage_daily_agg
(
    usage_date          Date,
    site_id             LowCardinality(String),
    site_name           String,
    partner_id          LowCardinality(String),
    partner_name        String,
    home_away           LowCardinality(String),
    channel             LowCardinality(String),
    customer_class      LowCardinality(String),
    collection_status   LowCardinality(String),

    event_count         UInt64,
    amount_expected     Decimal(18, 2),
    amount_billed       Decimal(18, 2),
    amount_collected    Decimal(18, 2),
    amount_discount     Decimal(18, 2),

    unbilled_count      UInt64,
    misrated_count      UInt64,
    unmatched_count     UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(usage_date)
ORDER BY (usage_date, site_id, channel, customer_class, collection_status, partner_id, home_away);

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv_usage_daily_agg
TO analytics.usage_daily_agg
AS SELECT
    toDate(e.event_ts)                              AS usage_date,
    e.site_id                                       AS site_id,
    s.site_name                                     AS site_name,
    e.partner_id                                    AS partner_id,
    p.partner_name                                  AS partner_name,
    multiIf(e.is_home_partner = 1, 'HOME', 'AWAY')  AS home_away,
    e.channel                                       AS channel,
    e.customer_class                                AS customer_class,
    e.collection_status                             AS collection_status,
    toUInt64(1)                                     AS event_count,
    e.amount_expected                               AS amount_expected,
    e.amount_billed                                 AS amount_billed,
    e.amount_collected                              AS amount_collected,
    e.amount_discount                               AS amount_discount,
    toUInt64(1 - e.is_billed)                       AS unbilled_count,
    toUInt64(1 - e.is_rated_correctly)              AS misrated_count,
    toUInt64(1 - e.is_matched)                      AS unmatched_count
FROM analytics.usage_event AS e
LEFT JOIN analytics.site    AS s ON s.site_id = e.site_id
LEFT JOIN analytics.partner AS p ON p.partner_id = e.partner_id;

-- ----------------------------------------------------------------------------
-- LAYER 2: site x day leakage rollup. Small enough to scan on every dashboard
-- load, which is why the leakage lens reads this instead of layer 1.
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analytics.leakage_daily_agg
(
    usage_date          Date,
    site_id             LowCardinality(String),
    site_name           String,

    event_count         UInt64,
    amount_expected     Decimal(18, 2),
    amount_collected    Decimal(18, 2),
    amount_leaked       Decimal(18, 2),

    unbilled_count      UInt64,
    misrated_count      UInt64,
    unmatched_count     UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(usage_date)
ORDER BY (usage_date, site_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv_leakage_daily_agg
TO analytics.leakage_daily_agg
AS SELECT
    toDate(e.event_ts)                              AS usage_date,
    e.site_id                                       AS site_id,
    s.site_name                                     AS site_name,
    toUInt64(1)                                     AS event_count,
    e.amount_expected                               AS amount_expected,
    e.amount_collected                              AS amount_collected,
    e.amount_expected - e.amount_collected          AS amount_leaked,
    toUInt64(1 - e.is_billed)                       AS unbilled_count,
    toUInt64(1 - e.is_rated_correctly)              AS misrated_count,
    toUInt64(1 - e.is_matched)                      AS unmatched_count
FROM analytics.usage_event AS e
LEFT JOIN analytics.site AS s ON s.site_id = e.site_id;
