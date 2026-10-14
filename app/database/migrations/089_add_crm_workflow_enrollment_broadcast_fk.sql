-- 089: outreach — the FK 058 promised. crm_workflow_enrollment's
-- source_broadcast_id (canon T20 col 18) now references crm_broadcast (088),
-- tenant-pinned: a run can only be stamped with its own merchant's broadcast.
-- 058's (merchant_id, source_broadcast_id) partial index already carries the
-- referencing side.
--
-- NOT VALID on purpose. No broadcast has ever existed, so every existing
-- run's source_broadcast_id is NULL and there is nothing to validate — while
-- validating would scan this hot table (the walker writes it constantly)
-- under a lock that holds those writes. A NOT VALID foreign key is still
-- enforced on every insert and update from this migration on.

ALTER TABLE crm_workflow_enrollment
    ADD CONSTRAINT crm_workflow_enrollment_broadcast_fk
    FOREIGN KEY (merchant_id, source_broadcast_id)
    REFERENCES crm_broadcast (merchant_id, id)
    NOT VALID;
