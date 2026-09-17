-- 073: crm_workflow_step (canon T26) — where a run has BEEN.
--
-- T20 holds the token's POSITION and nothing else: `SET current_node = $2`
-- overwrites the square it left, `without_reply` deletes the branch it
-- picked before the write, and up to _MAX_STEPS_PER_VISIT = 10 squares can
-- execute back to back with ONE update at the end — so a send -> condition
-- -> split -> call chain tells the database a single sentence ("she is on
-- settle now") and four squares leave no trace. This table is the trail
-- behind the token.
--
-- Append-only: rows are inserted BY the statement that moves the token
-- (the INSERT rides the lease-CAS as a CTE, so a step row exists if and
-- only if the move committed — no dual write, nothing to reconcile, no
-- reaper) and are NEVER updated. The walker NEVER reads this table.
--
-- The precedent is crm_decision_log (T14): "the one genuinely
-- unreconstructable table: the accountability bill the inversion runs up,
-- paid in rows." The gate keeps a decision log; the walker keeps none.
-- This is the walker's.
--
-- Three laws; two the DDL cannot enforce. The second is enforced below,
-- by trigger, not by intention (the 064 pattern):
--   1. The walker never reads it. current_node stays authoritative for
--      execution. The day anything on the hot path reads this table the
--      design is wrong — and that law is what keeps it droppable and
--      partitionable without touching execution.
--   2. Rows are never edited. No open row, no partial unique, nothing to
--      reconcile against current_node.
--   3. Every read is a UNION: the past is N closed rows, the present is
--      the run row plus node_arrived_at. That union is one function in
--      app/crm/outreach/steps.py; a consumer that forgets it shows a
--      timeline exactly one square short of the truth.
--
-- Vocabulary (arrived_by, outcome, node_type) carries NO CHECK — it grows
-- with NODE_TYPES and the edge labels an author writes, and vocabulary
-- lives in code dicts (the migration-027 scar). The CHECK below is on
-- FORMAT, which is required.

-- The pin target for the composite FK below (the 049/058 precedent):
-- every crm→crm FK is tenant-pinned, and a step row can never point
-- across a boundary. merchant_id first per the unique-index tenancy law.
-- The cost is one index tuple per run INSERTED: both columns are
-- immutable once written, so the walker's HOT updates never touch it.
-- Must exist BEFORE the CREATE TABLE — an inline FK requires its unique
-- target at constraint-creation time.
CREATE UNIQUE INDEX crm_workflow_enrollment_tenant_pin_ux
    ON crm_workflow_enrollment (merchant_id, id);

CREATE TABLE crm_workflow_step (
    id               bigserial PRIMARY KEY,
    merchant_id      text NOT NULL,
    enrollment_id    uuid NOT NULL,
    workflow_id      uuid NOT NULL,
    -- The version in force when the step CLOSED. A migrate publish can
    -- re-pin an open run between its arrival and its departure, so this
    -- may not be the version the token landed under. Node ids are minted
    -- once and never regenerated, and a migrate refuses to delete an
    -- occupied node, so rendering is safe either way; recording the
    -- arrival version too would mean a second column on the hot run row
    -- for a rare case.
    workflow_version integer NOT NULL,
    node             text NOT NULL,
    -- Denormalised so a 40-step timeline renders without resolving 40
    -- documents.
    node_type        text NOT NULL,
    arrived_at       timestamptz NOT NULL,
    -- Only CLOSED steps are written; the square the run stands on right
    -- now lives on the run row, which is what removes the update path.
    left_at          timestamptz NOT NULL,
    -- How the token got here: door (enrol) | timer (the alarm fired) |
    -- letter (an event woke it) | walk (chained from the previous square
    -- in the same visit).
    arrived_by       text NOT NULL,
    -- Why it left: the edge label, timeout, else, a split's arm, or an
    -- exit reason. NULL = a plain square with one arrow out.
    outcome          text,
    -- NULL = the run ended here.
    next_node        text,
    -- How many claims this square cost. attempts is only ever zeroed by
    -- the advance that flushes this row, so the number is exact — EXCEPT
    -- across a park: both resume paths zero it before any row exists, so
    -- a parked-then-resumed square flushes as one clean attempt. Ruled
    -- 17 Sep 2026: a park is visible only while it is happening (on the
    -- run row's status/last_error), not afterwards.
    attempts         smallint NOT NULL DEFAULT 1,
    last_error       text,
    -- What this square handed to a DISPATCHER, when it handed over
    -- anything: a call's lead (lead_call_tracker.id, status BACKLOG, for
    -- buddy's dispatch machine) or a send's manifest row (crm_message.id,
    -- status queued, for connectivity's dispatcher). Neither verb contacts
    -- anyone itself — each writes a row and returns, and this is the id of
    -- that row.
    dispatch_id      text,
    -- The letter that cut this square short, when one did: a POINTER
    -- into crm_event_raw, never a photocopy.
    --
    -- NOT the letter that STARTED the run. That one is run-level — the
    -- same id on every row of the trail — and lives once, in
    -- crm_workflow_enrollment.context.source_event_id, which is also what
    -- the founding-event dedupe reads. A reader already holds that row
    -- (only closed steps are written, so the open square comes from it),
    -- and putting it here would claim the founding letter ENDED the door
    -- square, which it did not: arrived_by = 'door' is what names it. Deliberately no FK — record
    -- owns that table and a schema-level reference across the module
    -- boundary would import the boundary into the DDL (the 050/059
    -- precedent), and crm_event_raw is partitioned with its own retention.
    cut_short_by     uuid,
    -- The house pin (049 merge-FK, 058 tenant_pin): every crm→crm FK is
    -- composite, so a step row can never point across a tenant boundary —
    -- against the tenant_pin_ux created above this table.
    CONSTRAINT crm_workflow_step_run_fk
        FOREIGN KEY (merchant_id, enrollment_id)
        REFERENCES crm_workflow_enrollment (merchant_id, id) ON DELETE CASCADE,
    CHECK (left_at >= arrived_at)
);

-- The per-run read, AND the index the cascade delete needs: a FK column
-- must LEAD its own index, or sweep_exited_runs_query seq-scans this
-- table once per run it removes.
CREATE INDEX crm_workflow_step_run_ix
    ON crm_workflow_step (enrollment_id, arrived_at);

-- The per-square funnel: "how many runs reached this square, and where did
-- they go from it" — merchant first (tenancy law), and a GROUP BY rather
-- than resolving every run's document.
CREATE INDEX crm_workflow_step_node_ix
    ON crm_workflow_step (merchant_id, workflow_id, node, left_at);

-- Law 2, enforced: the trail is written by the move that closed the
-- square and never edited — by trigger, not by intention (the 064
-- pattern). UPDATE-only: the trail dies with its run (the FK cascade
-- above), so no DELETE guard.
CREATE OR REPLACE FUNCTION crm_workflow_step_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'crm_workflow_step rows are append-only — the trail is written by the move that closed the square and never edited';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_workflow_step_immutable_guard
    BEFORE UPDATE ON crm_workflow_step
    FOR EACH ROW EXECUTE FUNCTION crm_workflow_step_immutable();

-- T20 col 19. The batch has to know where it started: this is the only
-- change to the run row, and the only place the CURRENT square's arrival
-- time lives until the step closes.
--
-- NULL for runs that pre-date this migration, deliberately — no backfill.
-- Defaulting to entered_at would be a lie for a run that has been walking
-- nine days, and it is exactly the kind of lie that ends up in a
-- merchant-facing "waiting since". A run with no stamp simply loses its
-- first closing row; every square after it is exact.
ALTER TABLE crm_workflow_enrollment
    ADD COLUMN node_arrived_at timestamptz;
