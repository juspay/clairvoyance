-- 071: the rider's chosen payment agent when several are ACTIVE.
-- Additive only. The booking path orders by preferred DESC, created_at DESC,
-- so an un-flagged customer keeps today's behaviour (newest drawable wins).
ALTER TABLE crm_customer_agent
    ADD COLUMN IF NOT EXISTS preferred boolean NOT NULL DEFAULT false;

-- At most one preferred agent per customer (merchant_id first: tenancy law).
CREATE UNIQUE INDEX IF NOT EXISTS crm_customer_agent_preferred_uq
    ON crm_customer_agent (merchant_id, customer_id) WHERE preferred;
