-- 088: outreach — crm_broadcast (canon T17, canon/06-outreach.md). Broadcasts
-- phase 1: the broadcast row itself. Its recipients (crm_broadcast_recipient,
-- T18) arrive in phase 2 with the freeze that fills them.
--
-- One deliberate execution: a segment admitted into a workflow, now or on a
-- schedule, the audience frozen late — just before dispatch. The launch
-- button and nothing else: it never sends (the workflow does) and never owns
-- the workflow (ten broadcasts may launch the same one).
--
-- The three references are tenant-pinned composite FKs (the 049/058/073
-- precedent): a broadcast can only freeze its own merchant's segment, launch
-- its own merchant's workflow and wear its own merchant's badge.

CREATE TABLE crm_broadcast (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id    text NOT NULL,
    name           text NOT NULL CHECK (name <> '' AND name !~ '^\s|\s$'),
    -- The badge (T22). Optional: a one-off blast needs no season.
    campaign_key   text,
    -- The audience — whose list to freeze, read once at resolved_at.
    segment_id     uuid NOT NULL,
    -- canon-mandated CHECK (T17 col 11: CK). resolving and dispatching stay
    -- split because they fail differently — stuck resolving is a segment
    -- problem, stuck dispatching a pipe problem.
    status         text NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'scheduled', 'resolving', 'dispatching',
                                     'paused', 'sent', 'closed', 'cancelled', 'failed')),
    -- The clocks: authored at created_at, aimed for scheduled_for, frozen at
    -- resolved_at (deliberately late), fired at dispatched_at, final at
    -- closed_at. The gaps between them are the design.
    scheduled_for  timestamptz,
    resolved_at    timestamptz,
    dispatched_at  timestamptz,
    closed_at      timestamptz,
    -- Who built the thing that will address 8,400 people.
    created_by     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- The action, always: every frozen recipient is enrolled into it.
    workflow_id    uuid NOT NULL,
    FOREIGN KEY (merchant_id, segment_id) REFERENCES crm_segment (merchant_id, id),
    FOREIGN KEY (merchant_id, workflow_id) REFERENCES crm_workflow (merchant_id, id),
    FOREIGN KEY (merchant_id, campaign_key) REFERENCES crm_campaign (merchant_id, key)
);

-- The pin target for tenant-pinned FKs into this table: a run's
-- source_broadcast_id (089) and, in phase 2, a recipient's broadcast_id.
CREATE UNIQUE INDEX crm_broadcast_tenant_pin_ux
    ON crm_broadcast (merchant_id, id);

-- The list read's cursor (newest first per merchant), carried by the index.
CREATE INDEX crm_broadcast_merchant_created_ix
    ON crm_broadcast (merchant_id, created_at DESC, id DESC);

-- canon T17 col 4 IX: the campaign-level read (broadcasts WHERE campaign_key
-- = K), and the FK's referencing side.
CREATE INDEX crm_broadcast_campaign_ix
    ON crm_broadcast (merchant_id, campaign_key, created_at DESC)
    WHERE campaign_key IS NOT NULL;
