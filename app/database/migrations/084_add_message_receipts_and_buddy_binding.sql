-- 084: connectivity — what delivery receipts and Buddy's binding need:
-- crm_message.pricing_category, crm_message_receipt_pending, and the
-- one-binding-per-merchant-and-channel index for Buddy's settings.


-- ===========================================================================
-- crm_message.pricing_category
-- Owner: connectivity.
-- ===========================================================================

-- crm_message.pricing_category (canon T16; inbox D8): what the provider
-- BILLED a message as.
--
-- Meta prices per message by category (marketing · utility · authentication,
-- and 'service' for a free-form reply inside the customer-service window),
-- and it can bill a template under a different category than the one it was
-- submitted with. The delivery receipt says which one it used, so the
-- receipts consumer records it here: cost reporting reads the provider's
-- verdict, not our guess.
--
-- Nullable and unchecked: NULL until a receipt says, and the vocabulary is
-- the provider's (the 027 scar — a new category is a deploy, never a
-- migration). Not in 056/060's immutability list, so the consumer may write
-- it once a receipt arrives.
ALTER TABLE crm_message
    ADD COLUMN IF NOT EXISTS pricing_category text;


-- ===========================================================================
-- crm_message_receipt_pending
-- Owner: connectivity.
-- ===========================================================================

-- crm_message_receipt_pending: a delivery receipt that arrived before its
-- message's row knew the provider's id.
--
-- The provider's id (a wamid) reaches crm_message when the dispatcher
-- records the send's outcome. A fast webhook can be filed and consumed
-- before that write lands, and then matches no row. Retrying the letter does
-- not help — the event worker offers it again on its very next pass, with no
-- delay, and quarantines it after a handful of tries — so the receipt is
-- parked HERE instead:
--
--   * every send path, right after stamping the id,
--     applies what is parked for it — in one atom, a row leaving only with
--     its receipt applied;
--   * the receipts consumer, after parking, tries its row once more, which
--     closes the window where both sides miss each other;
--   * the dispatcher's sweep drains the messages whose row now has the id,
--     then drops anything parked longer than the grace: a receipt that old
--     names a message this system did not send (the provider's own app on
--     the same binding), and it was never an error.
--
-- One row per (message, state): a duplicate receipt parks nothing new. Short
-- lived by design; nothing reads it but the two paths above.
CREATE TABLE crm_message_receipt_pending (
    merchant_id          text NOT NULL,
    provider_message_id  text NOT NULL,
    -- The manifest's word for what happened: sent · delivered · read · failed.
    state                text NOT NULL,
    occurred_at          timestamptz,
    error_code           text,
    pricing_category     text,
    parked_at            timestamptz NOT NULL DEFAULT now(),
    -- merchant_id first (the tenancy law): the provider's id arrives on a
    -- letter, so another tenant's id must never find this row.
    PRIMARY KEY (merchant_id, provider_message_id, state)
);

-- The sweep: everything parked past the grace, oldest first.
CREATE INDEX crm_message_receipt_pending_parked_ix
    ON crm_message_receipt_pending (parked_at);


-- ===========================================================================
-- crm_channel_binding: Buddy's binding
-- Owner: connectivity.
-- ===========================================================================

-- crm_channel_binding: Buddy answers on ONE binding per merchant and channel
-- (inbox R1, D25).
--
-- Buddy's settings (the agent, human handoff, the closing message and its
-- lead, the claim SLA, the non-text reply) live under
-- capabilities["conversation"] on the binding Buddy answers on, and nowhere
-- else: every other binding of the merchant does nothing in the inbox.
-- Choosing another binding MOVES the whole config in one atom
-- (connectivity/settings.py) — it is taken off the old binding and put on
-- the new one, under a lock on the merchant's bindings. This index is the
-- backstop that makes "two bindings both think they are Buddy's" impossible
-- whatever a writer does.
--
-- No status in the predicate: a paused binding keeps the config until the
-- merchant picks another, and the move clears it from wherever it is. Only
-- this change ever writes the key, so no existing row can violate this.
CREATE UNIQUE INDEX crm_channel_binding_buddy_uq
    ON crm_channel_binding (merchant_id, channel)
    WHERE capabilities ? 'conversation';
