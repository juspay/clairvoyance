-- 056: crm_message — one row per outbound send attempt, on any channel.
--
-- A blocked or failed attempt is still a row: this table is what answers
-- "why didn't my customer get the message", so an attempt that never reached
-- a provider has to be as visible as one that did.
--
-- Places this deliberately does NOT do the obvious thing:
--
-- 1. Not partitioned. A partitioned table's unique constraints must include
--    the partition key, which would break the dedupe index below. Partition
--    later, when volume actually calls for it.
-- 2. No format check on sent_to_address. It holds a phone number, an email or
--    an account id depending on the channel, so any check would have to
--    branch on channel — and channel values must stay changeable without a
--    migration. Writers normalise instead.
-- 3. No trigger forcing status to move forward only. Delivery receipts do
--    arrive out of order and will need that rule, but the dispatcher moves
--    rows backwards on purpose when it reclaims an abandoned one.
--
-- And two places this KNOWINGLY departed from the written table spec when
-- first recorded. RULED 29-Aug-2026: canon T16 was amended to adopt both, so
-- these notes are now the trail of how the spec got there, not open questions:
--
-- 4. customer_id carries a composite FK, where the spec says "No FK"
--    (justified there by partitioning + high volume: stamped at write, never
--    joined at read). We are not partitioned (see 1), so the FK costs little,
--    and it buys something real: without it a wrong merchant_id would file a
--    message against a customer belonging to another tenant, and every
--    journey read for that customer would then be wrong. Revisit when
--    partitioning lands — a composite FK is one of the things that makes
--    partitioning harder.
-- 5. No CHECK on source_kind, where the spec marks it CK with a fixed list
--    (broadcast · workflow · agent · transactional). The migration-027 scar
--    says vocabulary lives in code and never in a CHECK, because a CHECK is
--    exactly what made a new channel a migration rather than a deploy. We
--    followed the scar. NOTE the gap that leaves: "vocabulary in code" means
--    a code dictionary validates it somewhere, and today NOTHING validates
--    source_kind at all — that dictionary must land with the first producer.

CREATE TABLE crm_message (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         text NOT NULL,
    customer_id         uuid NOT NULL,
    -- Frozen at send time even as the customer's own contact details change,
    -- so an audit can always say where a message actually went.
    sent_to_address     text NOT NULL,
    -- Stored rather than derived from binding_id, because a message refused
    -- before a route was picked has no binding and permission is per channel.
    channel             text NOT NULL,
    -- Which configured endpoint it left on. Null on refused rows, and on
    -- every row until endpoints exist.
    binding_id          uuid,
    -- What caused this send, e.g. a broadcast, a workflow run, an agent
    -- reply, or the event that triggered it. Every funnel metric groups by
    -- these two.
    source_kind         text NOT NULL,
    source_id           uuid,
    -- The declared reason for contacting them, checked against what they
    -- agreed to. Not null because the permission check cannot answer without
    -- it, and an unanswerable check has to refuse.
    purpose_key         text NOT NULL,
    -- The provider renders the final text from these, so we never store a
    -- rendered string: it would only ever be our guess at their render.
    template_id         text,
    -- Plain jsonb accepts scalars and arrays too; the decoder drops anything
    -- that is not an object rather than the table rejecting the write.
    variables           jsonb NOT NULL DEFAULT '{}'::jsonb,
    --   queued -> sending -> blocked | accepted -> sent -> delivered -> read
    --   sending -> failed -> dead
    -- 'failed' is the provider refusing; 'dead' is us running out of retries.
    status              text NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'sending', 'blocked',
                                          'accepted', 'sent', 'delivered',
                                          'read', 'failed', 'dead')),
    -- Holds either our refusal (no_consent, quiet_hours) or the provider's
    -- error code. A row is refused by us or by them, never both, so a second
    -- column would always be null.
    reason              text,
    provider_message_id text,
    attempt             smallint NOT NULL DEFAULT 0,
    -- The provider's own charge from the delivery receipt, never our
    -- rate-card arithmetic.
    cost_micros         bigint,
    -- Points at the permission decision that authorised this send. No foreign
    -- key: that table is not on this branch yet.
    decision_id         bigint,
    -- Not null and no default: the producer must name the logical send.
    -- Every real producer has a natural key (the triggering event,
    -- enrolment:node), and an omittable column is a protection silently
    -- skipped.
    dedupe_key          text NOT NULL,
    -- Set while a worker holds the row; doubles as the lease that lets an
    -- abandoned row be spotted and requeued.
    claimed_at          timestamptz,
    -- When this row may next be attempted. now() for a new message, pushed
    -- into the future by a retry. The gate's quiet-hours deferral will write
    -- its next_allowed_at here too.
    next_attempt_at     timestamptz NOT NULL DEFAULT now(),
    -- No updated_at: every state below stamps its own timestamp, so a generic
    -- "something changed" would say less and cost a trigger on every write.
    created_at          timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz,
    delivered_at        timestamptz,
    -- Only channels that report reads ever fill this.
    read_at             timestamptz,
    -- Composite so a message can never be attached to another tenant's
    -- customer; a plain customer_id reference would accept any uuid.
    FOREIGN KEY (merchant_id, customer_id)
        REFERENCES crm_customer (merchant_id, id)
);

-- The only thing between a producer running twice and a customer getting the
-- same message twice. Total, not partial: the column is NOT NULL, so there is
-- no row this misses.
CREATE UNIQUE INDEX crm_message_merchant_dedupe_uq
    ON crm_message (merchant_id, dedupe_key);

-- The queue itself: due first, across all tenants. Filtering AND sorting on
-- next_attempt_at is what makes a retry wait its turn — on created_at it
-- would keep its original timestamp and jump ahead of every fresh message.
-- For a new row the two are equal, so FIFO fairness is unchanged.
CREATE INDEX crm_message_queued_ix
    ON crm_message (next_attempt_at)
    WHERE status = 'queued';

-- Finding rows whose worker never came back.
CREATE INDEX crm_message_claimed_ix
    ON crm_message (claimed_at)
    WHERE status = 'sending';

-- Analytics. Both are merchant-first so a tenant's slice is a range scan
-- rather than a filter over everyone's rows, and both carry created_at DESC
-- because every reporting question is time-bounded ("last 7 days") or
-- newest-first.
--
-- Serves: one merchant's messages, newest first · counts grouped by status
-- for a merchant over a window · platform-wide totals grouped by merchant,
-- which can aggregate in index order instead of sorting.
CREATE INDEX crm_message_merchant_created_ix
    ON crm_message (merchant_id, created_at DESC);

-- The per-customer drill-down behind a merchant's view of one person.
CREATE INDEX crm_message_merchant_customer_ix
    ON crm_message (merchant_id, customer_id, created_at DESC);

-- One real provider message is one row. A WRITE rule, not a read index, and
-- this table already writes the column — deferring it to the receipt walker
-- would land the rule after the writes it governs.
--
-- DELIBERATELY NOT merchant-scoped: the one exception to "merchant_id first
-- in every unique index". That law protects OUR identifiers, unique only
-- within a tenant. A provider id is unique worldwide, so scoping it per
-- merchant would let two tenants each record the same real message.
CREATE UNIQUE INDEX crm_message_provider_id_uq
    ON crm_message (provider_message_id)
    WHERE provider_message_id IS NOT NULL;

-- What was proposed is frozen; only the delivery envelope moves. An audit
-- record whose contents can be rewritten afterwards is a claim about what we
-- sent, not evidence of it — and intent alone does not stop an UPDATE.
CREATE OR REPLACE FUNCTION crm_message_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.id              IS DISTINCT FROM OLD.id
       OR NEW.merchant_id     IS DISTINCT FROM OLD.merchant_id
       OR NEW.customer_id     IS DISTINCT FROM OLD.customer_id
       OR NEW.sent_to_address IS DISTINCT FROM OLD.sent_to_address
       OR NEW.channel         IS DISTINCT FROM OLD.channel
       OR NEW.source_kind     IS DISTINCT FROM OLD.source_kind
       OR NEW.source_id       IS DISTINCT FROM OLD.source_id
       OR NEW.purpose_key     IS DISTINCT FROM OLD.purpose_key
       OR NEW.template_id     IS DISTINCT FROM OLD.template_id
       OR NEW.variables       IS DISTINCT FROM OLD.variables
       OR NEW.dedupe_key      IS DISTINCT FROM OLD.dedupe_key
       OR NEW.created_at      IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'crm_message: these fields are immutable and one of them changed — id, merchant_id, customer_id, sent_to_address, channel, source_kind, source_id, purpose_key, template_id, variables, dedupe_key, created_at';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_message_immutable_guard
    BEFORE UPDATE ON crm_message
    FOR EACH ROW EXECUTE FUNCTION crm_message_immutable();
