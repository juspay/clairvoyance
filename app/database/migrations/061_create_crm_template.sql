-- 060: crm_template (canon T23, C7) — the channel template registry. One
-- table for every channel that pre-registers message shapes: WhatsApp
-- (WABA templates) first, SMS-DLT and email later. crm_message.template_id
-- (T16, migration 056) stores the `name` this table registers — plain
-- text, no FK (crm_message is not partitioned by channel/language, so a
-- composite FK back here would need both and template_id carries neither).
--
-- Natural key is (merchant_id, channel, provider_account_ref, name,
-- language): Meta's real key is (WABA, name, language); merchant_id and
-- channel lead per tenancy law, but provider_account_ref (the WABA/DLT
-- account) must stay IN the key too — a merchant can hold more than one
-- installation per channel (multiple WABAs), and two installations
-- legitimately reusing the same name+language must not collide on one
-- row. provider_template_id is the wamid exception — Meta's id is
-- globally unique per provider, so it gets a partial unique index ALONE,
-- nullable until a draft is actually submitted.
--
-- provider_account_ref stays plain text (the WABA/DLT entity that owns
-- the template — templates are namespaced per provider account, not per
-- merchant) until C1 lands and it becomes a real
-- installation_id FK -> crm_connector_installation (T11); not this pass.
--
-- category/submitted_category/quality/status: all THEIRS, all current,
-- all NO CHECK (027 scar — provider vocabulary lives in code, Meta has
-- renamed categories before). Editing an approved template puts the SAME
-- row back to pending (Meta re-reviews in place) — history lives as
-- template.status events in the spine, replayable, not as extra rows here.

CREATE TABLE crm_template (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id          text NOT NULL,
    channel              text NOT NULL,
    provider_account_ref text NOT NULL,
    name                 text NOT NULL,
    language             text NOT NULL,
    provider_template_id text,
    category             text,
    submitted_category   text,
    category_updated_at  timestamptz,
    components           jsonb NOT NULL,
    status               text NOT NULL DEFAULT 'draft',
    status_updated_at    timestamptz NOT NULL DEFAULT now(),
    rejection_reason     text,
    quality              text NOT NULL DEFAULT 'UNKNOWN',
    quality_updated_at   timestamptz,
    last_synced_at       timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX crm_template_natural_uq
    ON crm_template (merchant_id, channel, provider_account_ref, name, language);

-- The wamid exception: globally unique per provider, so no merchant_id
-- prefix here — partial because a draft has no provider id yet.
CREATE UNIQUE INDEX crm_template_provider_id_uq
    ON crm_template (provider_template_id)
    WHERE provider_template_id IS NOT NULL;

CREATE INDEX crm_template_status_ix ON crm_template (status);

CREATE TRIGGER crm_template_touch
    BEFORE UPDATE ON crm_template
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();
