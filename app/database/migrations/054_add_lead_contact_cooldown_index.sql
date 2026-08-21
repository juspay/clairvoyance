-- Migration: Add contact-cooldown lookup indexes on lead_call_tracker
-- Description: Backs both branches of the `recent_contact_cooldown` pre-check's
-- count query (count_recent_contacted_leads_query), which asks "was this phone
-- number already contacted in the last N hours, for this merchant or for any
-- merchant under the reseller -- dialled, or being dialled right now?". That
-- query runs inside the dispatch hot path -- once per lead, per dispatch
-- attempt, before the dial -- so neither branch can be a sequential scan. No
-- prior index covers either: the dispatch index from migration 002 is
-- (status, is_locked, next_attempt_at), and the phone number inside `payload`
-- has never been indexed.
--
-- The count treats a row as "contact" when EITHER branch holds, so each
-- branch gets its own partial index. One combined index cannot serve both:
-- branch 1 sorts by call_initiated_time while branch 2 sorts by updated_at,
-- and a B-tree has a single key order.
--
--   BRANCH 1 (idx_lct_contact_cooldown): dialled-in-window rows --
--   `call_initiated_time IS NOT NULL AND call_initiated_time >= window`.
--   The IS NOT NULL predicate must match the query exactly or Postgres will
--   not use the partial index; it excludes every BACKLOG row, the bulk of
--   the table's churn.
--
--   BRANCH 2 (idx_lct_contact_cooldown_in_flight): rows being dispatched
--   right now -- `is_locked = TRUE OR status = 'PROCESSING'`,
--   freshness-bounded by `updated_at >= window` so a crashed worker's stale
--   lock stops counting once it ages past the window (a live row re-touches
--   updated_at on every mutation; an abandoned one does not). Locked BACKLOG
--   rows have NULL call_initiated_time, so they never appear in branch 1's
--   index -- branch 2 needs its own. Its population is tiny (at most one row
--   per in-flight dispatch) and rows move in/out constantly as locks flip --
--   intended, and cheap at this size.
--
-- The phone expression must match the query's expression exactly or Postgres
-- will not use either index. The query normalizes both sides of the
-- comparison to their last 10 digits
-- (RIGHT(regexp_replace(..., '\D', '', 'g'), 10)) -- same technique as
-- blacklisted_numbers.normalize_phone_number -- since stored numbers carry
-- inconsistent '+'/country-code formatting; both indexes are built on that
-- same normalized expression, not the raw payload value.
--
-- Column order in each index is (reseller_id, phone, <branch timestamp>):
-- both leading columns are always equality-filtered, and merchant_id is
-- deliberately NOT included because the "*" scope omits it entirely.
--
-- NOTE ON LOCKING: the migration runner wraps each file in a transaction, so
-- CREATE INDEX CONCURRENTLY cannot be used here. Plain CREATE INDEX blocks
-- writes to lead_call_tracker for the duration of each build. On a large
-- production table, run both CONCURRENTLY forms manually FIRST (outside a
-- transaction) and then apply this migration -- IF NOT EXISTS makes each a
-- no-op:
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_contact_cooldown
--       ON lead_call_tracker (
--           reseller_id,
--           (RIGHT(regexp_replace(payload->>'customer_mobile_number', '\D', '', 'g'), 10)),
--           call_initiated_time DESC
--       )
--       WHERE call_initiated_time IS NOT NULL;
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_contact_cooldown_in_flight
--       ON lead_call_tracker (
--           reseller_id,
--           (RIGHT(regexp_replace(payload->>'customer_mobile_number', '\D', '', 'g'), 10)),
--           updated_at DESC
--       )
--       WHERE is_locked = TRUE OR status = 'PROCESSING';
CREATE INDEX IF NOT EXISTS idx_lct_contact_cooldown
    ON lead_call_tracker (
        reseller_id,
        (RIGHT(regexp_replace(payload->>'customer_mobile_number', '\D', '', 'g'), 10)),
        call_initiated_time DESC
    )
    WHERE call_initiated_time IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_lct_contact_cooldown_in_flight
    ON lead_call_tracker (
        reseller_id,
        (RIGHT(regexp_replace(payload->>'customer_mobile_number', '\D', '', 'g'), 10)),
        updated_at DESC
    )
    WHERE is_locked = TRUE OR status = 'PROCESSING';
