-- 049: crm_customer (canon T05, A6) — the customer as ONE merchant knows
-- them; handles are columns. Created ONLY by resolve() — no other INSERT
-- into this table may exist anywhere (grep-enforceable).
--
-- Each partial unique index IS the duplicate detector: a collision at
-- insert means "resolve to the existing customer", never an error.
-- merchant_id is the first column of every unique index (tenancy law).
-- Handle changes follow the evidence ladder (ADR 0021); the history
-- trigger below means even a rogue UPDATE cannot destroy an old handle.

CREATE TABLE crm_customer (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         text NOT NULL,
    display_name        text,
    primary_locale      text,
    timezone            text,
    phone               text CHECK (phone ~ '^\+[1-9][0-9]{6,14}$'),
    email               text CHECK (email = lower(email)),
    igsid               text,
    shopify_customer_id text,
    external_ref        text,
    status              text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'merged_away', 'erased')),
    merged_into_id      uuid,
    merged_at           timestamptz,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Staples can never cross the tenant boundary or point at themselves:
    -- the composite FK pins the survivor to the SAME merchant (the plain
    -- id FK would let a raw UPDATE point at another tenant's row and
    -- corrupt every merge-following read).
    UNIQUE (merchant_id, id),
    CONSTRAINT crm_customer_no_self_merge
        CHECK (merged_into_id IS NULL OR merged_into_id <> id),
    FOREIGN KEY (merchant_id, merged_into_id)
        REFERENCES crm_customer (merchant_id, id)
);

CREATE UNIQUE INDEX crm_customer_merchant_phone_uq
    ON crm_customer (merchant_id, phone)
    WHERE status = 'active' AND phone IS NOT NULL;
CREATE UNIQUE INDEX crm_customer_merchant_email_uq
    ON crm_customer (merchant_id, email)
    WHERE status = 'active' AND email IS NOT NULL;
CREATE UNIQUE INDEX crm_customer_merchant_igsid_uq
    ON crm_customer (merchant_id, igsid)
    WHERE status = 'active' AND igsid IS NOT NULL;
CREATE UNIQUE INDEX crm_customer_merchant_shopify_uq
    ON crm_customer (merchant_id, shopify_customer_id)
    WHERE status = 'active' AND shopify_customer_id IS NOT NULL;
CREATE UNIQUE INDEX crm_customer_merchant_extref_uq
    ON crm_customer (merchant_id, external_ref)
    WHERE status = 'active' AND external_ref IS NOT NULL;

-- The customers list view: newest activity first, per merchant.
CREATE INDEX crm_customer_merchant_seen_ix
    ON crm_customer (merchant_id, last_seen_at DESC);

-- Merge-pair reads (id = $2 OR merged_into_id = $2) + FK maintenance
-- (canon marks merged_into_id FK·IX).
CREATE INDEX crm_customer_merged_into_ix
    ON crm_customer (merged_into_id)
    WHERE merged_into_id IS NOT NULL;

CREATE TRIGGER crm_customer_touch
    BEFORE UPDATE ON crm_customer
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();

-- ADR 0021 lock #4: when a handle column changes to a DIFFERENT non-NULL
-- value, the old value is appended into the attributes history under
-- _handle_history — by the table, not by discipline. Even a hand-written
-- UPDATE cannot lose an old handle.
CREATE OR REPLACE FUNCTION crm_customer_handle_history() RETURNS trigger AS $$
DECLARE
    col text;
    old_val text;
    new_val text;
BEGIN
    FOREACH col IN ARRAY
        ARRAY['phone', 'email', 'igsid', 'shopify_customer_id', 'external_ref']
    LOOP
        old_val := row_to_json(OLD)->>col;
        new_val := row_to_json(NEW)->>col;
        IF old_val IS NOT NULL AND new_val IS DISTINCT FROM old_val THEN
            NEW.attributes := jsonb_set(
                NEW.attributes,
                ARRAY['_handle_history'],
                COALESCE(NEW.attributes->'_handle_history', '[]'::jsonb)
                    || jsonb_build_object(
                        'handle', col,
                        'old', old_val,
                        'replaced_at', now()
                    )
            );
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_customer_handle_history_guard
    BEFORE UPDATE OF phone, email, igsid, shopify_customer_id, external_ref
    ON crm_customer
    FOR EACH ROW EXECUTE FUNCTION crm_customer_handle_history();
