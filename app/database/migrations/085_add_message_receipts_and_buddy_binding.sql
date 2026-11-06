-- 085: connectivity — what delivery receipts and Buddy's binding need:
-- crm_message.pricing_category, and the one-binding-per-merchant-and-channel
-- index for Buddy's settings.


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
-- (connectivity/accounts/channel_settings.py) — it is taken off the old binding and put on
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
