-- 092: outreach — crm_broadcast.attempts and last_error (canon T17,
-- canon/06-outreach.md). Broadcasts phase 2: the freeze's retry ladder.
--
-- The freeze's claim (outreach/broadcasts.py claim_due_freezes) re-takes a
-- broadcast left resolving once its lease runs out. Without a count, a
-- freeze that keeps breaking (a replica timeout, a bug) is claimed again
-- every lease, forever, and says nothing about why. attempts is spent BY
-- the claim, so a crash mid-freeze counts against the broadcast too; past
-- CRM_BROADCAST_FREEZE_MAX_ATTEMPTS the broadcast fails with last_error
-- readable on its row — the reason, not a log search.
--
-- Deliberate deviation from canon T17, with its reason: T17 has neither
-- column. They follow T20's attempts/last_error (canon/06-outreach.md T20
-- cols 15-16, 058): the same claim-spends-an-attempt ladder, for the same
-- job — a worker's row that must stop retrying and say why. Canon
-- amendment owed.

ALTER TABLE crm_broadcast
    ADD COLUMN attempts smallint NOT NULL DEFAULT 0,
    -- Why the last attempt broke, or why the broadcast failed: a segment
    -- that cannot be answered, or attempts run out.
    ADD COLUMN last_error text,
    -- FORMAT CHECK: a count never goes below zero.
    ADD CONSTRAINT crm_broadcast_attempts_ck CHECK (attempts >= 0);
