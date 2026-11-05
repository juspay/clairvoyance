-- 058: crm_workflow_enrollment (canon T20, W2) — one person's run through
-- a workflow: the board-game token, the only stateful row in outreach.
--
-- wake_at is the timer AND the lease (canon: "this is what replaces a
-- workflow engine"): the walker's claim pushes it forward one lease
-- window, so a dead worker's row self-heals when the clock passes again —
-- no reaper. The partial index below IS the walker's work queue.
--
-- attempts is incremented BY the claim — a poison run that crashes its
-- worker counts against itself; exhausted -> 'parked' with last_error
-- readable on the merchant's screen. Errors park, never exit.
--
-- context carries POINTERS to the spark plus the few small facts the
-- sends need ({source_event_id, phone, ...}) — never payload photocopies;
-- the event row already stores the letter verbatim.
--
-- Deliberate deviations from canon T20, each with its reason:
--   * exit_reason adds 'completed' (canon lists goal_met · timed_out ·
--     withdrawn · ejected): a run that executes its last node without the
--     goal firing is finished-without-converting — the funnel's
--     denominator. None of canon's four values is truthful for it;
--     canon amendment proposed to Swaroop 25 Aug 2026.
--   * the open-run unique is (merchant_id, workflow_id, enrollment_key),
--     not canon's (workflow_id, enrollment_key) — merchant_id must be
--     the first column of every unique index on crm_* tables (tenancy
--     law); workflow-scoped uniqueness is preserved.
--   * source_broadcast_id is a bare column: its canon FK targets
--     crm_broadcast (T17), which lands in phase 2 — the FK arrives with
--     that table's migration.

CREATE TABLE crm_workflow_enrollment (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         text NOT NULL,
    workflow_id         uuid NOT NULL,
    workflow_version    integer NOT NULL,
    customer_id         uuid NOT NULL,
    status              text NOT NULL DEFAULT 'waiting'
                        CHECK (status IN ('waiting', 'parked', 'exited')),
    current_node        text NOT NULL,
    wake_at             timestamptz,
    entered_at          timestamptz NOT NULL DEFAULT now(),
    exited_at           timestamptz,
    exit_reason         text
                        CHECK (exit_reason IN
                          ('goal_met', 'timed_out', 'withdrawn',
                           'ejected', 'completed')),
    context             jsonb NOT NULL DEFAULT '{}'::jsonb,
    enrollment_key      text NOT NULL,
    attempts            smallint NOT NULL DEFAULT 0,
    last_error          text,
    source_broadcast_id uuid,
    CONSTRAINT crm_workflow_enrollment_workflow_fk
        FOREIGN KEY (merchant_id, workflow_id)
        REFERENCES crm_workflow (merchant_id, id),
    -- an exited run always says why; a waiting run always has an alarm
    CHECK (status <> 'exited' OR exit_reason IS NOT NULL),
    CHECK (status <> 'waiting' OR wake_at IS NOT NULL)
);

CREATE INDEX crm_workflow_enrollment_merchant_ix
    ON crm_workflow_enrollment (merchant_id);

CREATE INDEX crm_workflow_enrollment_customer_ix
    ON crm_workflow_enrollment (customer_id);

-- THE walker's work queue: due tokens, keyset-ordered, nothing else.
CREATE INDEX crm_workflow_enrollment_due_ix
    ON crm_workflow_enrollment (wake_at, id)
    WHERE status = 'waiting';

-- One OPEN run per (workflow, key); key defaults to the customer id.
-- Keyed runs are how two open orders get two live flows (canon).
CREATE UNIQUE INDEX crm_workflow_enrollment_open_ux
    ON crm_workflow_enrollment (merchant_id, workflow_id, enrollment_key)
    WHERE status <> 'exited';

-- The retention sweep's read: exited rows age out on this index — most
-- of what keeps the hot table small (canon).
-- Canon's IX on source_broadcast_id (the FK waits for T17, phase 2).
CREATE INDEX crm_workflow_enrollment_broadcast_ix
    ON crm_workflow_enrollment (merchant_id, source_broadcast_id)
    WHERE source_broadcast_id IS NOT NULL;

CREATE INDEX crm_workflow_enrollment_retention_ix
    ON crm_workflow_enrollment (exited_at)
    WHERE status = 'exited';
