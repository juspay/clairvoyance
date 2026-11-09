-- 087: platform_identity holds Xyne user ids (owner: platform).
--
-- Xyne 4/5 sends to a person by their Xyne user id, and the gate asks the
-- book "did this address say STOP?" before every send. 048 lets the book
-- hold only phone · email · device, so a STOP on a Xyne id had nowhere to
-- live, and the probe skipped the kind it did not know — answering "not
-- suppressed" for a question nobody asked. The probe now fails closed on a
-- kind it cannot check (app/crm/platform/suppression.py); this lets the book
-- hold the one kind the send path needs. Only as suppression rows: resolve()
-- still registers phones and emails alone, so a Xyne id enters the book only
-- when someone records a STOP on it.
--
-- The kind list stays a CHECK, as 048 made it (canon T02 marks kind CK):
-- widened by one word here. The new kind gets the FORMAT check every kind
-- in the book has — the shape app/crm/shared/handles.py normalizes to — so
-- a STOP stored in any other form can never miss the probe.
--
-- NOT VALID, then validated by 088 in its own transaction: this table holds
-- a row per phone across every merchant, and adding a validated CHECK would
-- scan it under the ACCESS EXCLUSIVE lock this statement takes — every gate
-- probe would wait behind the scan. NOT VALID is enforced for every write
-- from this commit on; 088's VALIDATE scans under a lock that blocks neither
-- the probe nor a write.
--
-- lock_timeout: the ACCESS EXCLUSIVE lock is held for an instant, but WAITING
-- for it queues every gate probe behind the waiter — and a probe past its
-- deadline is a terminal 'blocked'. If a long reader holds the table, this
-- migration fails after 3 s and is re-run, instead of stalling every send.

SET LOCAL lock_timeout = '3s';

ALTER TABLE platform_identity
    DROP CONSTRAINT platform_identity_kind_check,
    ADD CONSTRAINT platform_identity_kind_check
        CHECK (kind IN ('phone', 'email', 'device', 'xyne_user_id')) NOT VALID,
    ADD CONSTRAINT platform_identity_xyne_user_id_shape
        CHECK (kind <> 'xyne_user_id' OR value ~ '^[A-Za-z0-9_-]{1,64}$') NOT VALID;
