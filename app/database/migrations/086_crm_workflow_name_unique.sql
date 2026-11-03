-- Migration: one non-archived workflow per (merchant, name)
-- Description: a workflow's name is how a provisioner finds it. Nautilus
-- clones each Shopify merchant's order-confirmation plan from a base plan
-- and adopts the merchant's existing one by name, so two creates racing for
-- the same shop (two orders a moment apart) made two live plans and called
-- the customer twice. The template door already refuses a second template
-- of the same name with a 409 that the provisioner adopts; this gives the
-- workflow door the same answer.
--
-- Partial on status <> 'archived': archiving a plan frees its name (archived
-- is terminal, so a freed name can never collide again). A draft holds its
-- name — a create whose publish failed is adopted, not cloned twice.
-- lower(name): "Order Confirmation" and "order confirmation" are one name to
-- the person reading the console. merchant_id first (tenancy law).
--
-- Before applying: the index cannot build while duplicates exist. Check with
--
--   SELECT merchant_id, lower(name), count(*)
--   FROM crm_workflow WHERE status <> 'archived'
--   GROUP BY 1, 2 HAVING count(*) > 1;
--
-- and archive or rename the extras first.

CREATE UNIQUE INDEX IF NOT EXISTS crm_workflow_name_ux
    ON crm_workflow (merchant_id, lower(name))
    WHERE status <> 'archived';
