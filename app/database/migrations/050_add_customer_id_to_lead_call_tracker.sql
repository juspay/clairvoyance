-- 050: lead_call_tracker.customer_id (ADR 0017, A15) — the stamp.
--
-- One nullable column: buddy's push/answer handlers call resolve() and
-- stamp the id at write time; the journey's call arm joins on it, never
-- by phone-matching at read. NULL is truthful (pre-CRM rows and calls
-- whose resolve hasn't run) — forward-only, no backfill.
--
-- Deliberately NO foreign key: a schema-level reference from buddy's
-- table into a crm table would import the boundary into the DDL.

ALTER TABLE lead_call_tracker ADD COLUMN customer_id uuid;

-- The journey's call-arm read is WHERE customer_id = $1 ORDER BY
-- created_at — the cursor rides IN the index (an equality-only partial
-- index is half an index).
-- Plain CREATE INDEX briefly blocks lead writes during the build scan —
-- accepted deliberately at current table size (seconds, same as every
-- existing index here); CONCURRENTLY can't run in the transactional
-- runner and building a non-transactional path for this is not worth it
-- yet. Revisit when indexing the big partitioned tables.
CREATE INDEX lead_call_tracker_customer_ix
    ON lead_call_tracker (customer_id, created_at)
    WHERE customer_id IS NOT NULL;
