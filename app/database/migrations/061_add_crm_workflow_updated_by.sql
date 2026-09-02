-- 061: crm_workflow.updated_by — who last changed the plan.
--
-- 057 gave the table created_by and updated_at, which can say WHEN a plan
-- last changed and WHO first made it, but never who made the change. A plan
-- is a shared document, so crediting the creator for someone else's edit is
-- exactly the small lie an audit column exists to prevent.
--
-- Set by the three writes that mean "someone changed this plan" — draft
-- save, publish, status change — beside the updated_at they already touch.
-- Not by creation: created_by answers that.
--
-- Nullable, no backfill, no default. NULL means "we did not record it", the
-- true state of every row written before this migration; the console shows
-- those as a bare timestamp. No index — it is read as part of a row already
-- fetched, never searched on.

ALTER TABLE crm_workflow ADD COLUMN updated_by text;
