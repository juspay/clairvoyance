-- Migration 049: Make the console's phone search an indexed probe.
--
-- Analytics phone search matches on digits only, because stored numbers
-- carry '+'/country-code prefixes the caller cannot be expected to type
-- (see get_analytics_filters in queries/breeze_buddy/analytics/analytics.py):
--
--     regexp_replace(payload->>'customer_mobile_number', '\D', '', 'g')
--         LIKE '%<digits>%'
--
-- Two things make that unindexable by a btree: the predicate is over a
-- computed expression, and the pattern has a leading wildcard. So every
-- phone search seq-scans lead_call_tracker and re-runs the regexp per row —
-- cost grows with the whole table, not with the number of matches.
--
-- A GIN trigram index over the *same* expression fixes both: pg_trgm can
-- serve a leading-wildcard LIKE, and indexing the expression means the
-- normalisation is precomputed. Semantics are unchanged — this adds an
-- access path, it does not alter what matches.
--
-- IMPORTANT: the index expression must stay character-identical to the
-- predicate built in analytics.py, or the planner will silently stop using
-- it and quietly fall back to the seq scan.
--
-- pg_trgm is already installed (migration 034).
--
-- DEPLOY NOTE: this builds a plain (non-CONCURRENT) index, which holds a
-- SHARE lock on lead_call_tracker — reads continue, writes block until the
-- build finishes. On a large lead_call_tracker, apply during a low-write
-- window. CONCURRENTLY is not an option here: scripts/migrate.py runs each
-- migration inside a transaction, and Postgres rejects CREATE INDEX
-- CONCURRENTLY in a transaction block. Switching to it needs a
-- non-transactional path in the runner first.
--
-- Measured locally on 20k synthetic rows:
--   before  Seq Scan          120.8 ms   929 shared buffers
--   after   Bitmap Index Scan   5.1 ms    40 shared buffers
-- The gap widens with table size: the seq scan is O(rows), the probe is not.
--
-- Note: trigram matching needs at least 3 characters to use the index;
-- shorter searches fall back to the previous behaviour, which is fine —
-- a 1-2 digit phone search is not a meaningful query.

CREATE INDEX IF NOT EXISTS idx_lead_call_tracker_customer_mobile_digits_trgm
    ON lead_call_tracker USING gin (
        (regexp_replace(payload->>'customer_mobile_number', '\D', '', 'g'))
        gin_trgm_ops
    );
