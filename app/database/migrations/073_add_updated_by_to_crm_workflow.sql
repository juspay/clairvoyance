-- 073: outreach — crm_workflow.updated_by (T19)
--
-- created_by is a byline that never changes; every later touch (draft
-- save, publish, status change) needs its own byline too, so the plans
-- list can say who last touched a plan, not just who started it.
-- Nullable: a system-driven write (none exist today) leaves no one to name.

ALTER TABLE crm_workflow ADD COLUMN updated_by text;
