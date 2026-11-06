-- Migration 084: covering index for merchant + date analytics on lead_call_tracker
-- Description: every analytics widget (build_analytics_where_clause) filters
--   execution_mode IN (...) AND call_initiated_time >= $1 AND < $2 AND merchant_id = $3
-- and nothing indexes (merchant_id, call_initiated_time) together — the 028
-- composite has reseller_id/outcome between them, so the planner falls back to
-- the single-column date index. Incident 2026-10-12 (LB 5xx, replica 25 s
-- timeouts): one merchant, one day kept ~2.4k rows and discarded ~244k by
-- filter. Proven ~100x waste; not proven to be the sole cause of the stall.
--
-- execution_mode is INCLUDE, not a key: IN (...) would split the scan, and
-- the call-details list omits that filter. INCLUDE carries the columns the
-- widget CTEs select so they run as Index Only Scans. Wide columns (payload,
-- meta_data, template) are deliberately left out.
--
-- FOLLOW-UP (nothing dropped here): idx_lead_call_tracker_merchant_identifier
-- (018, single-column merchant_id) is a strict prefix of this and should be
-- redundant — confirm with pg_stat_user_indexes, then drop in its own migration.
--
-- PRODUCTION NOTE (054/075 precedent): the runner wraps each file in a
-- transaction, so CONCURRENTLY cannot be used here, and the plain form blocks
-- writes to the dispatch hot table for the build. Run this on the primary
-- FIRST, outside a transaction, then apply the migration (IF NOT EXISTS = no-op):
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_merchant_initiated_covering
--       ON lead_call_tracker (merchant_id, call_initiated_time)
--       INCLUDE (execution_mode, request_id, status, outcome, attempt_count,
--                call_end_time, call_direction, id);
--
-- A failed concurrent build leaves an INVALID index of this name; DROP INDEX
-- CONCURRENTLY it and re-run before applying.

CREATE INDEX IF NOT EXISTS idx_lct_merchant_initiated_covering
    ON lead_call_tracker (merchant_id, call_initiated_time)
    INCLUDE (
        execution_mode,
        request_id,
        status,
        outcome,
        attempt_count,
        call_end_time,
        call_direction,
        id
    );
