-- Migration: index a plan's own runs — by time, by key, and the open ones
-- Description: the console reads a workflow's runs constantly — the Runs
-- table (list_runs, ORDER BY entered_at DESC, id DESC, keyset-paged), the
-- run numbering CTE (PARTITION BY enrollment_key ORDER BY entered_at, id),
-- the summary's runs-per-day and open-by-node rollups, the calls summary's
-- id sweep, and now the funnel's run_endings_in_window. Every one of them
-- is `merchant_id = $1 AND workflow_id = $2` with a window on entered_at.
--
-- Nothing indexed that pair with a date. crm_workflow_enrollment_merchant_ix
-- is (merchant_id) alone — one merchant's every run across every plan —
-- and the open_ux partial (merchant_id, workflow_id, enrollment_key) only
-- covers runs that have not exited, which is the opposite of what a
-- historical window asks for. So a merchant with many plans pays a scan of
-- all their runs to draw one plan's table.
--
-- (merchant_id, workflow_id, entered_at, id) is tenancy-first (canon), and
-- its trailing (entered_at, id) IS the sort both directions of the console's
-- reads use — a B-tree walks backwards for the DESC page just as cheaply,
-- so one index carries the newest-first table and the oldest-first
-- numbering without a second copy.
--
-- PRODUCTION NOTE (the 054 precedent): migrations run inside a transaction,
-- so CREATE INDEX CONCURRENTLY cannot be used here, and a plain CREATE
-- INDEX holds ACCESS EXCLUSIVE for the build — on a large enrollment table
-- that stalls the walker's claims. Run the concurrent form manually FIRST,
-- outside a transaction, then apply this migration: IF NOT EXISTS makes it
-- a no-op that only records the version.
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS crm_workflow_enrollment_plan_ix
--       ON crm_workflow_enrollment (merchant_id, workflow_id, entered_at, id);

CREATE INDEX IF NOT EXISTS crm_workflow_enrollment_plan_ix
    ON crm_workflow_enrollment (merchant_id, workflow_id, entered_at, id);

-- Two more reads the console makes on every page, each with its own shape:
--
-- 1. "Run 3 of 3" — list_runs numbers a run among ITS KEY's runs of the
--    plan with two correlated counts per page row (never a window over
--    the whole plan). Those counts want (merchant_id, workflow_id,
--    enrollment_key) then (entered_at, id) to count "up to this one" from
--    the index alone. The open_ux partial has the first three but only
--    for runs that have not exited; numbering is over every run.
--
-- 2. "Where open runs are" (open_by_node) and the publish-time
--    occupied-squares check (occupied_nodes) both read a plan's open runs
--    grouped by current_node. A plan's history grows without bound; its
--    open set does not, so a partial index on the open rows keeps the read
--    proportional to what it returns. The predicate is spelled
--    `status <> 'exited'` — the CHECK admits only waiting / parked /
--    exited, and both readers spell it the same way so the planner can
--    prove the partial applies.
--
-- Same production note as above: build each CONCURRENTLY first, then apply.
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS crm_workflow_enrollment_key_ix
--       ON crm_workflow_enrollment (merchant_id, workflow_id, enrollment_key, entered_at, id);
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS crm_workflow_enrollment_open_node_ix
--       ON crm_workflow_enrollment (merchant_id, workflow_id, current_node)
--       WHERE status <> 'exited';

CREATE INDEX IF NOT EXISTS crm_workflow_enrollment_key_ix
    ON crm_workflow_enrollment (merchant_id, workflow_id, enrollment_key, entered_at, id);

CREATE INDEX IF NOT EXISTS crm_workflow_enrollment_open_node_ix
    ON crm_workflow_enrollment (merchant_id, workflow_id, current_node)
    WHERE status <> 'exited';
