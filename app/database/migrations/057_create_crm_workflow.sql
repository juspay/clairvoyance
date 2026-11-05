-- 057: crm_workflow (canon T19, W1) — the plan: ONE document a walker
-- reads live. Many customers walk the same one; edits reach everyone not
-- yet past them.
--
-- definition jsonb is THE plan, whole: {entry, nodes, edges, goal, exits}
-- — sections of one document, never columns. Node ids are minted once at
-- creation and NEVER regenerated: enrollment.current_node resolves
-- against this live document, and a regenerated id strands every waiting
-- run (enforced by the publish validator in outreach/plans.py, not DDL —
-- graph shape is vocabulary, and vocabulary lives in code).
--
-- draft is Dittofeed's two-column trick: edits land here; publish copies
-- draft -> definition and bumps version. A half-finished edit can never
-- become the document walkers read. version is an AUDIT stamp ("she
-- entered under v3"), never an execution pin.
--
-- status CHECK is canon-mandated (T19 keys column: CK). paused = no new
-- enrolments AND the walker skips its runs; archived = open runs
-- force-exited as 'ejected'.

CREATE TABLE crm_workflow (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id text NOT NULL,
    name        text NOT NULL,
    definition  jsonb,
    draft       jsonb,
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'live', 'paused', 'archived')),
    version     integer NOT NULL DEFAULT 0,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    -- a flow may only run once a published document exists
    CHECK (status = 'draft' OR definition IS NOT NULL)
);

CREATE INDEX crm_workflow_merchant_ix ON crm_workflow (merchant_id);

-- The entry-rule processor's read: live plans, per merchant, every tick.
CREATE INDEX crm_workflow_status_ix ON crm_workflow (merchant_id, status);

-- Tenant-pinned FK target (the 049 merge-FK precedent): enrollment rows
-- reference (merchant_id, id), so a run can never point across a tenant
-- boundary. merchant_id first per the unique-index tenancy law.
CREATE UNIQUE INDEX crm_workflow_tenant_pin_ux ON crm_workflow (merchant_id, id);
