-- 085: crm_customer_handle — the customer's ids that are ROWS, not columns
-- (owner: identity). Written ONLY by resolve(), like crm_customer.
--
-- crm_customer keeps its five handle columns (phone, email, igsid,
-- shopify_customer_id, external_ref) exactly as 049 made them. A channel id
-- that arrives later — a Xyne user id first, then Slack — is a row here, so
-- the next channel is an entry in app/crm/shared/handles.py and NOT a
-- column, an index and a trigger edit on the customer table. The vocabulary
-- of kinds lives in that file; this table checks FORMAT only.
--
--   kind    which kind of id ('xyne_user_id')
--   scope   the account the id is unique within ('' when the provider's
--           ids are unique on their own, as Xyne's are; a Slack workspace
--           or an Instagram account when they are not)
--   value   the id itself, already normalized by the writer
--
-- One live owner per id: crm_customer_handle_live_value_uq. One live id per
-- customer per kind per scope: crm_customer_handle_live_slot_uq — so two
-- racing writers can never leave a customer with two, whatever the code
-- does. Both are partial on released_at IS NULL: a replaced id is RELEASED,
-- never rewritten, and a released row blocks nobody (the 049 history
-- trigger's job, done as rows: ADR 0021 lock #4).
--
-- A row never changes hands. On a staple the loser's live rows are released
-- — they stay on the loser as its audit + undo copies, as its columns do —
-- and the survivor gets rows of its own. So "which ids did this record
-- hold, and when" is always answerable from the rows alone. The guard below
-- freezes everything a row says; DELETE stays possible on purpose, for
-- erasure.
--
-- merchant_id is the first column of every unique index (tenancy law), and
-- the composite FK pins a handle to a customer of the SAME merchant.
--
-- New, empty table: nothing existing is altered or locked.

CREATE TABLE crm_customer_handle (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id  text NOT NULL,
    customer_id  uuid NOT NULL,
    kind         text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{0,62}$'),
    scope        text NOT NULL DEFAULT ''
                 CHECK (scope = btrim(scope) AND length(scope) <= 256),
    value        text NOT NULL
                 CHECK (value <> '' AND value = btrim(value) AND length(value) <= 256),
    created_at   timestamptz NOT NULL DEFAULT now(),
    released_at  timestamptz,
    CONSTRAINT crm_customer_handle_release_after_create
        CHECK (released_at IS NULL OR released_at >= created_at),
    FOREIGN KEY (merchant_id, customer_id)
        REFERENCES crm_customer (merchant_id, id)
);

CREATE UNIQUE INDEX crm_customer_handle_live_value_uq
    ON crm_customer_handle (merchant_id, kind, scope, value)
    WHERE released_at IS NULL;

CREATE UNIQUE INDEX crm_customer_handle_live_slot_uq
    ON crm_customer_handle (merchant_id, customer_id, kind, scope)
    WHERE released_at IS NULL;

-- Every row of a record, released ones included: the history read, and the
-- index the foreign key needs (the slot index above covers live rows only).
CREATE INDEX crm_customer_handle_customer_ix
    ON crm_customer_handle (merchant_id, customer_id);

-- What a row says never changes: merchant, customer, kind, scope, value and
-- created_at are frozen, and a released row stays released, untouched. The
-- only update a live row takes is its release. Even a hand-written UPDATE
-- cannot rewrite the history.
CREATE OR REPLACE FUNCTION crm_customer_handle_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.released_at IS NOT NULL THEN
        RAISE EXCEPTION 'crm_customer_handle %: a released handle is history and cannot change', OLD.id;
    END IF;
    IF NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
        OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.scope IS DISTINCT FROM OLD.scope
        OR NEW.value IS DISTINCT FROM OLD.value
        OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'crm_customer_handle %: merchant_id, customer_id, kind, scope, value and created_at never change', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_customer_handle_guard
    BEFORE UPDATE ON crm_customer_handle
    FOR EACH ROW EXECUTE FUNCTION crm_customer_handle_guard();
