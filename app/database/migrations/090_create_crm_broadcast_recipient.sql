-- 090: outreach — crm_broadcast_recipient (canon T18, canon/06-outreach.md).
-- Broadcasts phase 2: the admission queue under a broadcast, shipped with the
-- writer that fills it — the freeze (outreach/broadcasts.py freeze_broadcast).
--
-- One row per person the segment held when the broadcast was frozen; each
-- row later dies at a guard (skipped, with its reason) or becomes a run
-- (enrolled) — phase 3. Why "the number was smaller than expected" always has
-- an answer. A pure child row with no merchant_id (canon: it rides the
-- broadcast), every read and write entered through the already-scoped parent.
-- The composite PK IS the dedupe: a crashed freeze re-runs and a person
-- already queued is a no-op (ON CONFLICT DO NOTHING), never a second row.
--
-- customer_id is PK·IX in the canon, not an FK (T21's member row is the one
-- with the FK): the freeze is the only writer, and it reads its people from
-- the merchant's own segment.

CREATE TABLE crm_broadcast_recipient (
    broadcast_id  uuid NOT NULL REFERENCES crm_broadcast (id),
    -- The freeze stores WHO. Where — her current handle — is live, read at
    -- admission (phase 3); the run's manifest records what was really used.
    customer_id   uuid NOT NULL,
    -- canon-mandated CHECK (T18 col 8: CK). The queue's own life, nothing
    -- more: sends, refusals and receipts live on the run's manifest rows.
    status        text NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'claimed', 'skipped', 'enrolled', 'cancelled')),
    -- Pre-enrolment deaths only (already_in_flow, cooldown_active, resolve
    -- failures) — vocabulary in code, never a CHECK. Gate verdicts are never
    -- recorded here.
    skip_reason   text,
    -- Which worker holds the row, and since when (phase 3's claim).
    claimed_by    text,
    claimed_at    timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (broadcast_id, customer_id),
    -- A reason exactly when skipped: a skip without one is unexplained, a
    -- reason on any other row is a lie.
    CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))
);

-- canon T18 col 3 IX: which broadcasts reached this customer.
CREATE INDEX crm_broadcast_recipient_customer_ix
    ON crm_broadcast_recipient (customer_id);

CREATE TRIGGER crm_broadcast_recipient_touch
    BEFORE UPDATE ON crm_broadcast_recipient
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();

-- The freeze's claim (outreach/broadcasts.py claim_due_freezes): broadcasts
-- whose moment has come, and freezes whose worker died mid-way. Partial, so
-- it holds only the few broadcasts in flight, never the history.
CREATE INDEX crm_broadcast_due_ix
    ON crm_broadcast (scheduled_for)
    WHERE status IN ('scheduled', 'resolving');
