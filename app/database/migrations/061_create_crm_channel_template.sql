-- 061: crm_channel_template (canon T23, C7) — the channel template registry.
--
-- One table for every channel that pre-registers message shapes: WhatsApp
-- (WABA templates) first, SMS-DLT second, email later. crm_message.template_id
-- (T16, migration 056) stores the `name` this table registers — plain text, no
-- FK, because crm_message carries a name and no language, and a composite FK
-- would need both.
--
-- Named crm_channel_TEMPLATE, singular and channel-qualified, because "template"
-- alone already means something else in this repo: buddy's agent templates are
-- JSON conversation graphs, and two things called template in one schema is a
-- question every future reader has to re-answer.
--
-- Deliberate choices, each with the reason it beat the obvious alternative:
--
-- 1. The natural key carries provider_account_ref, one column beyond canon
--    T23's (merchant_id, channel, name, language). The provider's own key is
--    (account, name, language) — for WhatsApp, (WABA, name, language) — and a
--    merchant may hold TWO accounts on one channel (two shops, two WABAs).
--    Without the account in the key, the same name+language registered in the
--    second account collides with the first and simply cannot be created,
--    though they are two different templates with two different provider ids.
--    merchant_id still leads, as every unique index on a crm_ table must.
--
-- 2. provider_template_id gets a partial unique index ALONE — the same
--    exception crm_message_provider_id_uq earns. That law protects OUR
--    identifiers, which are unique only inside a tenant; this is the
--    PROVIDER's, globally unique by construction, and webhooks arrive naming
--    it and nothing else. Partial because a draft has no provider id yet.
--
-- 3. No CHECK on status, category, quality or channel. All four are provider
--    vocabulary, and the 027 scar says vocabulary lives in code: Meta has
--    renamed categories before and will add statuses we have not seen. The
--    lowercase dictionary (draft · submitting · pending · approved · rejected ·
--    paused · deleted) is enforced by the one place that writes it — the
--    provider face normalises there, so an unknown word arrives lowercased
--    rather than rejected.
--
--    'submitting' is ours, not canon's: it is the exclusive claim a submit
--    holds while the provider call is in flight, so two callers cannot both
--    register one template. Vocabulary lives in code, so adding it costs
--    nothing here.
--
-- 4. category and submitted_category are two columns on purpose. Providers
--    re-categorise templates on their own, and MARKETING becoming UTILITY
--    changes what the merchant is billed. One column would overwrite the
--    evidence that it ever happened; two make the difference visible, which
--    is the whole reason canon T23 keeps both.
--
-- 5. Editing an approved template puts the SAME row back to pending — Meta
--    re-reviews in place. History lives as template.status events in the
--    spine, replayable, not as extra rows here.
--
-- 6. Every timestamp column is a stamp, not a derived value: status_updated_at
--    is also the out-of-order guard for webhooks (a provider promises no
--    ordering, and a status LADDER would be wrong here — see choice 5, where
--    approved -> pending is a legitimate move backwards, so time is the only
--    honest ordering key).
--
-- 7. last_synced_at ships with NO WRITER, and that is canon's own position:
--    freshness comes from webhooks, one write per actual change, and there is
--    deliberately no timer walking every merchant's templates on a clock. The
--    column exists for a future explicit "import this account's existing
--    templates" action, which is a human pressing a button, never a schedule.
--    Every other column is written by either the lifecycle routes or the
--    webhook consumer.

CREATE TABLE crm_channel_template (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id          text NOT NULL,
    -- whatsapp, sms, email… validated against the CONNECTORS registry in
    -- code at create time, never by this table (see 3).
    channel              text NOT NULL,
    -- The provider account that owns this template — a WABA id today. Plain
    -- text rather than an FK to crm_connector_installation: the template is
    -- namespaced by the PROVIDER's account, which outlives any particular
    -- installation row (re-onboarding writes a new row for the same WABA).
    provider_account_ref text NOT NULL,
    -- What crm_message.template_id stores.
    name                 text NOT NULL,
    language             text NOT NULL,
    -- NULL until the provider accepts a submission (see 2).
    provider_template_id text,
    -- THEIRS (see 4).
    category             text,
    -- OURS: what we asked for.
    submitted_category   text,
    category_updated_at  timestamptz,
    -- The provider's registered structure, verbatim. Never validated against
    -- their schema here: a second validator refuses shapes they accept.
    components           jsonb NOT NULL,
    status               text NOT NULL DEFAULT 'draft',
    status_updated_at    timestamptz NOT NULL DEFAULT now(),
    -- The provider's own words, kept unparaphrased — it is what the merchant
    -- has to act on to get an approval.
    rejection_reason     text,
    quality              text NOT NULL DEFAULT 'UNKNOWN',
    quality_updated_at   timestamptz,
    -- See 7: no writer today.
    last_synced_at       timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- The natural key (see 1).
CREATE UNIQUE INDEX crm_channel_template_natural_uq
    ON crm_channel_template (merchant_id, channel, provider_account_ref, name,
                             language);

-- The provider-id exception (see 2). Webhooks arrive naming this and nothing
-- else, so it has to be findable without a tenant.
CREATE UNIQUE INDEX crm_channel_template_provider_id_uq
    ON crm_channel_template (provider_template_id)
    WHERE provider_template_id IS NOT NULL;

-- The console's list is (merchant, channel, status); the send-time lookup is
-- (merchant, channel, account, name) and is served by the natural-key index's
-- prefix, so it needs nothing of its own.
CREATE INDEX crm_channel_template_merchant_status_ix
    ON crm_channel_template (merchant_id, channel, status);

CREATE TRIGGER crm_channel_template_touch
    BEFORE UPDATE ON crm_channel_template
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();
