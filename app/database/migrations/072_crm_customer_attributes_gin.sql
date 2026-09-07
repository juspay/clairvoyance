-- 072: crm_customer.attributes as a keyed store for other modules.
--
-- A module may keep per-customer state under one key of ``attributes``
-- (agentic keeps its onboarding attempts under "agents"). Most lookups carry
-- the customer id; the rare ones that do not (a Juspay agent_id arriving on
-- a webhook) find the customer by jsonb containment on the whole document:
--   WHERE attributes @> '{"agents": [{"agent_id": "cont_…"}]}'
-- jsonb_path_ops indexes exactly that operator, and nothing else, so it is
-- the smallest index that turns the scan into a lookup.
CREATE INDEX IF NOT EXISTS crm_customer_attributes_gin
    ON crm_customer USING gin (attributes jsonb_path_ops);
