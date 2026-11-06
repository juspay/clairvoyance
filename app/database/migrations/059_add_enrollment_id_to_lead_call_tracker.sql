-- 059: lead_call_tracker.enrollment_id (ADR 0010, W3) — the stamp that
-- ties a call to the workflow run that caused it.
--
-- One nullable column: the walker's call node creates a normal buddy lead
-- (today's dispatch machine, unchanged — ADR 0010: voice stays outside
-- the gate, governed by its existing checks) and stamps the run's id so
-- funnels join runs <-> calls. NULL is truthful: every non-workflow lead
-- (nautilus pushes, inbound, playground) never has one. Forward-only,
-- no backfill.
--
-- Deliberately NO foreign key: a schema-level reference from buddy's
-- table into a crm table would import the boundary into the DDL (the 050
-- customer_id precedent, same reasoning).

ALTER TABLE lead_call_tracker ADD COLUMN enrollment_id uuid;

-- The funnel's read is WHERE enrollment_id = ANY(runs of flow X) — the
-- partial index keeps it off the fat table's back. Plain CREATE INDEX
-- accepted at current table size (the 050 precedent and reasoning).
CREATE INDEX lead_call_tracker_enrollment_ix
    ON lead_call_tracker (enrollment_id)
    WHERE enrollment_id IS NOT NULL;
