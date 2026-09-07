-- 070: agentic — crm_customer_agent
--
-- A rider's standing UPI mandate (Juspay AOP "agent"). One row per
-- onboarding attempt; the newest ACTIVE one with all identifiers is the
-- drawable agent. The draws made against it are NOT a table: they live on
-- the chat session they happened in (chat_session.metadata.uap_draws, one
-- element per Juspay order, aggregated by the rider_ref template var) and
-- mirror into crm_event_raw.

CREATE TABLE IF NOT EXISTS crm_customer_agent (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         text NOT NULL,
    customer_id         uuid NOT NULL,
    juspay_customer_id  text,                      -- cth_/cst_ … from /v2/customers
    agent_obj_ref       text NOT NULL,             -- ours, per attempt
    action_obj_ref      text,                      -- ours
    agent_id            text,                      -- cont_… from Juspay
    agent_ref_id        text,                      -- OB-… (NPCI)
    action_id           text,                      -- cont_… from Juspay
    action_ref_id       text,                      -- ARID-…
    payer_avpa          text,                      -- rider's UPI id (SDK result); masked on the page
    agentic_app         text,
    status              text NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','ACTIVE','FAILED','PAUSED','REVOKED','EXPIRED')),
    action_status       text,
    intent_constraints  jsonb,                     -- approved rule (rider-edited) or our proposal
    meta                jsonb NOT NULL DEFAULT '{}'::jsonb,
    verified_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- tenant-pinned FK: a row can never point at another merchant's customer
    FOREIGN KEY (merchant_id, customer_id) REFERENCES crm_customer (merchant_id, id)
);

-- agent_obj_ref is OURS, minted unique per attempt, and the webhook and the
-- patch path look a row up by it alone — so it is globally unique: a
-- collision could never route another tenant's event onto this row.
CREATE UNIQUE INDEX IF NOT EXISTS crm_customer_agent_ref_uq
    ON crm_customer_agent (agent_obj_ref);
CREATE INDEX IF NOT EXISTS crm_customer_agent_customer_ix
    ON crm_customer_agent (merchant_id, customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS crm_customer_agent_agent_id_ix
    ON crm_customer_agent (agent_id) WHERE agent_id IS NOT NULL;
