-- Migration: a run's lane — hot (act now) or cold (held overnight by a window)
-- Description: every wait square with a calling window moves a timer that
-- ends after the hours to the next opening, and every run held that night
-- lands on the same second (09:00:00). On 21 Sep 2026 that was 6,256 runs
-- for one plan, each queuing a call the moment the walker reached it, on a
-- number that places 23 calls a minute. The pile was ejected by hand.
--
-- `lane` records that a run was held ("cold"); the walker claims hot runs
-- first and cold runs only into the lines the plan's numbers have free
-- (outreach/capacity.py). A merchant letter that wakes or refreshes the run
-- flips it back to hot: the customer acted now. So does queuing its call:
-- from there the run holds a line and its bookkeeping must not wait behind
-- the pile. Stored, not derived: "was held" is history no predicate can
-- recompute from the row once the alarm has passed. Closed enum, so a CHECK
-- is allowed (vocabulary lives in code; FORMAT laws in the database).
--
-- Two partial indexes, one per reader:
--   lane_due_ix   (lane, wake_at, id) WHERE waiting — the hot claim's ORDER
--                 BY, and the in-flight range (cold rows a claim just pushed
--                 one lease ahead) the room arithmetic subtracts;
--   cold_pile_ix  (merchant_id, workflow_id, node_arrived_at, id) WHERE
--                 waiting AND cold — the cold claim goes plan by plan in
--                 ARRIVAL order: the run held at 21:05 is dialled before the
--                 one held at 06:00, not by a wake_at every pile row shares.
-- The older crm_workflow_enrollment_due_ix (wake_at, id) stays for every
-- other reader of due rows.
--
-- On prod: CREATE INDEX CONCURRENTLY first, then apply this file (IF NOT
-- EXISTS makes the second create a no-op). Rows already holding a window's
-- opening alarm when this deploys carry the default 'hot' — set them cold
-- once, on the plan's own clock, or the first morning claims the whole pile
-- as hot (docs/crm/runbooks/overnight-drain.md).

ALTER TABLE crm_workflow_enrollment
    ADD COLUMN IF NOT EXISTS lane text NOT NULL DEFAULT 'hot'
        CHECK (lane IN ('hot', 'cold'));

CREATE INDEX IF NOT EXISTS crm_workflow_enrollment_lane_due_ix
    ON crm_workflow_enrollment (lane, wake_at, id)
    WHERE status = 'waiting';

CREATE INDEX IF NOT EXISTS crm_workflow_enrollment_cold_pile_ix
    ON crm_workflow_enrollment (merchant_id, workflow_id, node_arrived_at, id)
    WHERE status = 'waiting' AND lane = 'cold';
