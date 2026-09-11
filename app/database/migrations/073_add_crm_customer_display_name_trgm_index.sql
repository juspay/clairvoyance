-- Migration 073: index the console's customer-name search.
--
-- list_customers_query's display_name ILIKE ('%q%') is a seq scan over
-- crm_customer (N11) — the docstring already flagged pg_trgm as the
-- follow-up.
--
-- Plain trgm index per F/02's ruling, not a composite (merchant_id,
-- display_name) one — that would need btree_gin to mix the equality column
-- with a trigram op class, and F/02 chose the simpler plain index instead.
--
-- CAVEAT (unverified, no DB available to EXPLAIN against): the ILIKE sits
-- as one arm of an OR against phone/email in list_customers_query, with
-- merchant_id/status as outer conjuncts. Whether the planner actually picks
-- this index over a seq scan depends on it choosing a BitmapOr across all
-- three OR arms — that has not been measured. Migration 053's scar applies
-- here too: if the planner doesn't pick it up, it fails silently, not
-- loudly. Confirm with EXPLAIN ANALYZE against real data before relying on
-- this for latency, and revisit (partial index matching the arms' shared
-- status='active' predicate, or a composite via btree_gin) if it doesn't
-- get used.
--
-- Note: trigram matching needs at least 3 characters to use the index, so
-- 1-2 character searches keep the previous seq-scan behaviour. That is
-- fine — a 2-letter name search is not a meaningful query.
--
-- pg_trgm is already installed (migration 047); CREATE EXTENSION repeated
-- here anyway so this migration is self-contained.
--
-- DEPLOY NOTE: plain (non-CONCURRENT) build, per migration 053's note —
-- scripts/migrate.py runs each migration inside a transaction, and Postgres
-- rejects CREATE INDEX CONCURRENTLY inside one. Holds a SHARE lock on
-- crm_customer (reads continue, writes block) until the build finishes, so
-- apply during a low-write window. Build time was not measured on a
-- production-sized crm_customer (no such database available here) — check
-- the row count before applying, and if the lock would outlast the window,
-- do not hand-run CONCURRENTLY here: it needs a non-transactional path in
-- the runner first (053 records the same prerequisite).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS crm_customer_display_name_trgm
    ON crm_customer USING gin (display_name gin_trgm_ops);
