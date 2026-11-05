-- 064: outreach — crm_workflow_version (ADR 0023, rollout phase 11): the
-- immutable per-publish document a run is pinned to.
--
-- 057 keeps ONE live definition per plan and calls `version` an audit
-- stamp. ADR 0023 reverses that: every enrollment executes the version it
-- entered under (crm_workflow_enrollment.workflow_version becomes the
-- execution pin), so each publish must leave a document that never
-- changes again. crm_workflow.definition stays the LATEST version's
-- document — the entry consumer and the console read it; the walker
-- (phase 12) reads this table by (workflow_id, version).
--
-- on_publish is a closed status enum -> CHECK required (law 11):
--   pin      new entrants take this version, runs in flight finish theirs;
--   migrate  every open run is re-pinned to this version inside the
--            publish atom, allowed only when the stranding validator
--            passes — 057's semantics as an opt-in mode.
--
-- merchant_id first in the unique index (tenancy law); the FK pins the
-- tenant (the 057 tenant_pin precedent). No partitioning: a plan publishes
-- tens of versions, not millions of rows.

CREATE TABLE crm_workflow_version (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id  text NOT NULL,
    workflow_id  uuid NOT NULL,
    version      integer NOT NULL,
    definition   jsonb NOT NULL,
    on_publish   text NOT NULL DEFAULT 'pin'
                 CHECK (on_publish IN ('pin', 'migrate')),
    published_by text,
    published_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT crm_workflow_version_workflow_fk
        FOREIGN KEY (merchant_id, workflow_id)
        REFERENCES crm_workflow (merchant_id, id)
);

CREATE UNIQUE INDEX crm_workflow_version_uq
    ON crm_workflow_version (merchant_id, workflow_id, version);

-- A version a run references is never mutated — by trigger, not by
-- intention (the 051 pattern). Only rows, never edits; the retention
-- sweep (phase 14) deletes unreferenced old versions, so no DELETE guard.
CREATE OR REPLACE FUNCTION crm_workflow_version_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.merchant_id  IS DISTINCT FROM OLD.merchant_id
       OR NEW.workflow_id  IS DISTINCT FROM OLD.workflow_id
       OR NEW.version      IS DISTINCT FROM OLD.version
       OR NEW.definition   IS DISTINCT FROM OLD.definition
       OR NEW.on_publish   IS DISTINCT FROM OLD.on_publish
       OR NEW.published_by IS DISTINCT FROM OLD.published_by
       OR NEW.published_at IS DISTINCT FROM OLD.published_at THEN
        RAISE EXCEPTION 'crm_workflow_version rows are immutable — publish writes a new version, never edits one';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_workflow_version_immutable_guard
    BEFORE UPDATE ON crm_workflow_version
    FOR EACH ROW EXECUTE FUNCTION crm_workflow_version_immutable();

-- Backfill 1: every plan with a published document gets its current
-- version row, so the pin resolves for runs entered under it. Older
-- versions were overwritten in place by 057 and cannot be recovered.
INSERT INTO crm_workflow_version (merchant_id, workflow_id, version, definition)
SELECT merchant_id, id, version, definition
  FROM crm_workflow
 WHERE definition IS NOT NULL;

-- Backfill 2: open runs entered under an OLDER, unrecoverable version are
-- re-pinned to the current one. That is exactly what they execute today
-- (057: the live document), so nothing changes for them — and when the
-- walker starts honouring the pin (phase 12) none of them parks on a
-- version that no longer exists.
UPDATE crm_workflow_enrollment e
   SET workflow_version = w.version
  FROM crm_workflow w
 WHERE w.merchant_id = e.merchant_id
   AND w.id = e.workflow_id
   AND w.definition IS NOT NULL
   AND e.status <> 'exited'
   AND e.workflow_version <> w.version;
