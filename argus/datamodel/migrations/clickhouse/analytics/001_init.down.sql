-- Drop in reverse dependency order: materialized views before their targets,
-- so ClickHouse never holds a view pointing at a table that no longer exists.
DROP VIEW IF EXISTS analytics.mv_leakage_daily_agg;
DROP VIEW IF EXISTS analytics.mv_usage_daily_agg;
DROP TABLE IF EXISTS analytics.leakage_daily_agg;
DROP TABLE IF EXISTS analytics.usage_daily_agg;
DROP TABLE IF EXISTS analytics.usage_event;
DROP TABLE IF EXISTS analytics.product;
DROP TABLE IF EXISTS analytics.partner;
DROP TABLE IF EXISTS analytics.site;
