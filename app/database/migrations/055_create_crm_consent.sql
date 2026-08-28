-- 055: permission — crm_consent_event + crm_consent_state (T07/T08, canon/03-permission.md) — the two consent stores.
--
-- The event table is the legal record: prove she agreed, possibly years later.
-- The state table is the resolved answer the send gate reads on every message.
-- record_consent() writes both in one transaction, which is the only reason
-- they can never disagree. They ship together for the same reason.

-- The permission slips, as they were given.
--
-- address is the handle the consent was collected against, and it is required:
-- permission belongs to a way of reaching someone, not to the person. When a
-- number is reassigned to a stranger, this is what stops the new holder
-- inheriting the old holder's opt-in. It cannot be filled in later.
-- There is no EXPIRE event type: expiry is arithmetic on the state table, and
-- no human performed it.
-- channel and purpose_key carry no CHECK: both grow with the product, and a
-- constraint means a new value cannot be used until someone writes a migration
-- (the 027 scar, building-modules law 11). The vocabulary lives in schemas.py.
--
-- What makes that safe is that the READ models type them as plain str while
-- ConsentEventIn keeps the enums — so the API refuses an unknown value at the
-- front door, and a row written around the module still decodes rather than
-- breaking every later read for that customer.
--
-- event_type and status DO keep theirs: closed status enums, not vocabularies,
-- which law 11 requires a CHECK on.
CREATE TABLE crm_consent_event (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id  text NOT NULL,
    customer_id  uuid NOT NULL,
    address      text NOT NULL,
    event_type   text NOT NULL
                 CHECK (event_type IN ('REQUEST', 'GRANT', 'WITHDRAW',
                                       'IMPORT', 'CONFIRM')),
    channel      text NOT NULL,
    purpose_key  text NOT NULL,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    artifact_ref text,

    -- Composite, not REFERENCES crm_customer (id). Both ids arrive in the same
    -- request body, and a plain id FK accepts any existing uuid — so a wrong
    -- merchant_id would plant a real customer's withdrawal in a tenant that
    -- never reads it. 049 declares UNIQUE (merchant_id, id) for exactly this.
    --
    -- RESTRICT: a customer with consent history cannot be hard-deleted, because
    -- this ledger is the answer to "prove she agreed" years later. Erasure is
    -- the soft path (crm_customer.status = 'erased'), not a DELETE.
    FOREIGN KEY (merchant_id, customer_id)
        REFERENCES crm_customer (merchant_id, id) ON DELETE RESTRICT
);

-- One customer's slips, newest first.
CREATE INDEX crm_consent_event_merchant_customer_ix
    ON crm_consent_event (merchant_id, customer_id, occurred_at DESC);

-- Nothing may edit or remove a slip. A correction is a new event, never an
-- edit, because the question this table answers is "what did she actually
-- agree to, and when" — and an edited answer is not evidence.
CREATE OR REPLACE FUNCTION crm_consent_event_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'crm_consent_event is append-only: % refused. A correction is a new '
        'event (WITHDRAW/CONFIRM), never an edit.', TG_OP;
END;
$$;

CREATE TRIGGER crm_consent_event_no_update
    BEFORE UPDATE ON crm_consent_event
    FOR EACH ROW EXECUTE FUNCTION crm_consent_event_append_only();

CREATE TRIGGER crm_consent_event_no_delete
    BEFORE DELETE ON crm_consent_event
    FOR EACH ROW EXECUTE FUNCTION crm_consent_event_append_only();


-- The resolved answer, one row per question the gate can ask.
--
-- The primary key spells out what permission means here: one merchant, one
-- customer, one channel, one purpose.
-- There is no `expired` status. Expiry is checked against the clock on every
-- read, so it cannot be briefly wrong between a grant lapsing and a job
-- noticing. A refusal is stored rather than inferred: never having been asked
-- and having been told no are different answers, and only one can be fixed by
-- asking again.
-- expires_at is the single clock a row may carry; what it means follows from
-- the status.
-- last_event_id points at the slip that produced this row, so this table can be
-- discarded and rebuilt from the event table alone.
-- The gate reads here, so tolerant decoding matters most on this table.
CREATE TABLE crm_consent_state (
    merchant_id   text NOT NULL,
    customer_id   uuid NOT NULL,
    channel       text NOT NULL,
    purpose_key   text NOT NULL,
    status        text NOT NULL
                  CHECK (status IN ('granted', 'withdrawn',
                                    'prohibited', 'pending_confirm')),
    expires_at    timestamptz,
    last_event_id uuid REFERENCES crm_consent_event (id),
    PRIMARY KEY (merchant_id, customer_id, channel, purpose_key),
    -- RESTRICT, not CASCADE. A cascade here would be unreachable anyway — the
    -- ledger's FK refuses the customer delete first, and the append-only trigger
    -- refuses clearing the ledger to get past it — so it only advertised an
    -- erasure path that does not exist. Both tables now say the same thing.
    FOREIGN KEY (merchant_id, customer_id)
        REFERENCES crm_customer (merchant_id, id) ON DELETE RESTRICT
);

-- No secondary index here on purpose. The gate's probe filters on
-- (merchant_id, customer_id, channel), which is a leading prefix of the primary
-- key, so the PK's own btree already serves it — a separate index would be a
-- second copy maintained on every write for nothing.
--
-- An expires_at index is also deliberately absent. Nothing queries it yet, and
-- because the upsert rewrites expires_at on every repeat write, indexing that
-- column would make each of those a non-HOT update: new entries in every index
-- instead of none. It can be added with the re-ask sweep that needs it, shaped
-- for that query — probably (merchant_id, expires_at) rather than a global one.
