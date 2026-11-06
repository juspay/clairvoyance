-- 060: the connector layer — crm_connector_installation + crm_channel_binding.
--
-- Two tables in one migration because they are meaningless apart: a binding
-- carries a foreign key into an installation, and an installation with no
-- binding is an account nothing can send from. Splitting them would only
-- create a numbered window in which the schema is half-built.
--
--   crm_connector_installation   the DOOR — a merchant's account on one
--                                connector (a WhatsApp Business Account).
--   crm_channel_binding          the PIPE — one real endpoint under that
--                                account (a phone number, a sender id, a
--                                from-address).
--
-- A message resolves pipe -> door -> secret, in that order, and every step is
-- merchant-scoped so no lookup can wander into another tenant's rows.
--
-- Deliberate choices, each with the reason it beat the obvious alternative:
--
-- 1. No CHECK on connector_key or on channel. Those columns are vocabulary,
--    and the migration-027 scar says vocabulary lives in code: a CHECK is
--    exactly what once made adding a channel a migration instead of a deploy.
--    The adapter registry (app/crm/connectivity/providers/__init__.py) is the
--    one place the list is enforced, and adding email means adding a file
--    there — no schema change at all.
--
-- 2. status IS a CHECK on both tables, and that is not a contradiction.
--    connector_key names WHICH provider (open set, grows with the product);
--    status names WHERE IN ITS LIFECYCLE a row is (closed set, changes only
--    when the lifecycle itself changes). Same split as crm_message.status.
--
-- 3. No 'expired' status. Expiry is a predicate — token_expires_at < now() —
--    and a stored copy of a predicate is a lie waiting to happen: it is
--    correct only until the clock passes it and something remembers to run.
--    NULL means the token does not expire (some providers issue permanent
--    ones); Meta system-user tokens run ~60 days, so a refresh job later
--    watches the non-NULL rows.
--
-- 4. credential_id is a real foreign key, ON DELETE RESTRICT. The vault is a
--    legacy table and pointing at it couples us to its shape, which is a cost
--    worth paying: without the constraint, deleting a credential through the
--    existing credentials API silently breaks every send for that merchant,
--    and the only symptom is messages failing with "credential missing" long
--    after the delete. RESTRICT turns that into an immediate, explicit
--    refusal at the moment somebody tries.
--
--    RESTRICT and never CASCADE: deleting a secret must not quietly delete
--    the record that a merchant ever connected. Disconnecting is a status
--    change ('revoked'), not a DELETE — history is the point of this table.
--
--    Nullable, so an installation may exist before its bundle does (a
--    half-finished onboarding is 'connecting' with no credential yet); NULL
--    is exempt from the constraint, and send() refuses such a row anyway.
--
-- 5. health_detail and capabilities are jsonb blobs rather than columns. The
--    health probe ladder (configured -> authenticated -> subscribed ->
--    heartbeat -> healthy) will grow reasons we cannot name yet, and read
--    receipts / session windows / throughput budgets differ per channel and
--    per provider tier. Code asks the blob instead of hardcoding "WhatsApp
--    has read receipts" — which is how the second channel stays cheap.
--    INTERIM: template language rides capabilities.template_language until
--    T23's template registry lands; then the registry decides per template.
--
-- 6. No format CHECK on `address`. It means whatever its channel says it
--    means: a Meta phone_number_id here, a sender id or a from-address
--    elsewhere. Any check would have to branch on channel and so re-introduce
--    the vocabulary this table refuses to store. Same reasoning as
--    crm_message.sent_to_address; writers normalise instead.
--
-- 7. EVERY index below is a UNIQUE one — a correctness rule, not a read
--    optimisation. These tables are configuration: one row per merchant per
--    account, one per endpoint, so tens of thousands of rows at the very top
--    end. At that size a sequential scan is sub-millisecond, and a plain index
--    would only add write cost and a page to keep warm for a scan Postgres was
--    never going to struggle with.
--
--    Three candidates were considered and dropped on purpose, so nobody has to
--    rediscover the reasoning:
--      · (merchant_id, connector_key) — a PREFIX of the account unique index,
--        which already serves it. Redundant at any table size.
--      · (credential_id) — the FK's referencing side. Postgres scans here on
--        every credentials DELETE, but that is a rare admin action against a
--        tiny table.
--      · (token_expires_at) — for a refresh job that does not exist yet. An
--        index ships with the query that needs it, never before.
--    If one of these tables ever stops being configuration-sized, add the
--    index THEN, with the plan that proves it.

-- ---------------------------------------------------------------------------
-- The door
-- ---------------------------------------------------------------------------

CREATE TABLE crm_connector_installation (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         text NOT NULL,
    -- Which provider this account is on: whatsapp, sms, instagram, email…
    -- Validated by the adapter registry, never by this table (see 1).
    connector_key       text NOT NULL,
    -- The provider's own id for the account — a WhatsApp Business Account id
    -- today. Composite-unique with merchant so one merchant can hold two
    -- accounts on the same connector (two shops, two WABAs).
    external_account_id text NOT NULL,
    -- What the merchant calls it in the console. Cosmetic, never matched on.
    display_label       text,
    -- Where the secret bundle lives, not the secret (see 4). One vault row
    -- holds a whole bundle, so rotation is a new row plus a pointer flip
    -- here — reversible in one statement. RESTRICT: a credential in use by a
    -- live connection cannot be deleted out from under it.
    credential_id       uuid REFERENCES credentials (id) ON DELETE RESTRICT,
    status              text NOT NULL DEFAULT 'connecting'
                        CHECK (status IN ('connecting', 'healthy', 'degraded',
                                          'revoked', 'disabled')),
    -- NULL means permanent (see 3).
    token_expires_at    timestamptz,
    -- Stamped by arriving provider events. Catches the failure mode a token
    -- check cannot see: credentials still valid, webhook subscription quietly
    -- dropped, and nothing inbound for days.
    last_event_at       timestamptz,
    health_detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    installed_at        timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- One installation per merchant per provider account. merchant_id leads, as
-- every unique index on a crm_ table must: the ids after it are only unique
-- inside a tenant.
--
-- It also serves the send path's lookup (merchant_id, connector_key), which is
-- a prefix of these columns — so a separate index on that pair would be pure
-- write cost for a scan Postgres can already do here.
CREATE UNIQUE INDEX crm_connector_installation_account_uq
    ON crm_connector_installation (merchant_id, connector_key, external_account_id);

-- Not a read path: this exists so crm_channel_binding can carry a COMPOSITE
-- foreign key and be structurally unable to point at another tenant's
-- installation. Postgres requires a unique index to reference. It happens to
-- serve the "this merchant's installation by id" fetch too.
CREATE UNIQUE INDEX crm_connector_installation_merchant_id_uq
    ON crm_connector_installation (merchant_id, id);

CREATE TRIGGER crm_connector_installation_touch
    BEFORE UPDATE ON crm_connector_installation
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();

-- ---------------------------------------------------------------------------
-- The pipe
-- ---------------------------------------------------------------------------
--
-- Two more choices that belong to this table alone:
--
-- 8. 'paused' fails sends CLOSED. A paused pipe is not a slow pipe: the
--    resolver refuses rather than picking another route, because silently
--    sending from a different number is worse than not sending.
--
-- 9. 'retired' rows are NEVER deleted. crm_message.binding_id points here,
--    and "which number did this August send leave on" must stay answerable
--    long after a provider recycles that number to somebody else.

CREATE TABLE crm_channel_binding (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id     text NOT NULL,
    -- whatsapp, sms, email, instagram… validated in code (see 1).
    channel         text NOT NULL,
    installation_id uuid NOT NULL,
    -- The provider's identifier for this endpoint (see 6).
    address         text NOT NULL,
    capabilities    jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Which pipe an unrouted message takes. Partial-unique below, so a
    -- merchant can never have two primaries on one channel.
    is_primary      boolean NOT NULL DEFAULT false,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'retired')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- Composite, so a binding is structurally unable to hang off another
    -- tenant's installation. A plain installation_id reference would accept
    -- any uuid, and the merchant scope would rest on the writer being right.
    FOREIGN KEY (merchant_id, installation_id)
        REFERENCES crm_connector_installation (merchant_id, id)
);

-- One row per real endpoint per merchant.
CREATE UNIQUE INDEX crm_channel_binding_merchant_address_uq
    ON crm_channel_binding (merchant_id, channel, address);

-- Exactly one default pipe per channel. Partial, so the non-primary rows are
-- unconstrained — a merchant may hold many numbers, but only one answers
-- "send this WhatsApp message" when nothing named a route.
CREATE UNIQUE INDEX crm_channel_binding_primary_uq
    ON crm_channel_binding (merchant_id, channel)
    WHERE is_primary;

-- One live endpoint belongs to ONE merchant, platform-wide.
--
-- The SECOND deliberate exception to "merchant_id first in every unique
-- index", and it earns it the same way crm_message_provider_id_uq does: that
-- law protects OUR identifiers, which are unique only inside a tenant. An
-- address is the PROVIDER's identifier. Inbound routing works backwards
-- through this index — a delivery receipt or a customer reply names only the
-- receiving number, and that number is how we learn whose message it was — so
-- two merchants registering one number would make the sender unknowable.
--
-- Scoped to non-retired rows on purpose: providers recycle numbers, and a
-- retired row must be able to coexist with the same address live under
-- whoever holds it now (see 9).
CREATE UNIQUE INDEX crm_channel_binding_address_uq
    ON crm_channel_binding (channel, address)
    WHERE status <> 'retired';

CREATE TRIGGER crm_channel_binding_touch
    BEFORE UPDATE ON crm_channel_binding
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();

-- ---------------------------------------------------------------------------
-- crm_message trigger amendment — rides this migration because merged
-- migrations are never edited (056 shipped with #1031).
--
-- binding_id becomes SET-ONCE: NULL until a route is picked, stamped when
-- the message leaves, never rewritten after — a rewrite would forge which
-- endpoint a message left on, while full immutability would forbid the
-- stamp itself (T16 col 6). Everything else is unchanged from 056.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION crm_message_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.id              IS DISTINCT FROM OLD.id
       OR NEW.merchant_id     IS DISTINCT FROM OLD.merchant_id
       OR NEW.customer_id     IS DISTINCT FROM OLD.customer_id
       OR NEW.sent_to_address IS DISTINCT FROM OLD.sent_to_address
       OR NEW.channel         IS DISTINCT FROM OLD.channel
       OR (OLD.binding_id IS NOT NULL
           AND NEW.binding_id IS DISTINCT FROM OLD.binding_id)
       OR NEW.source_kind     IS DISTINCT FROM OLD.source_kind
       OR NEW.source_id       IS DISTINCT FROM OLD.source_id
       OR NEW.purpose_key     IS DISTINCT FROM OLD.purpose_key
       OR NEW.template_id     IS DISTINCT FROM OLD.template_id
       OR NEW.variables       IS DISTINCT FROM OLD.variables
       OR NEW.dedupe_key      IS DISTINCT FROM OLD.dedupe_key
       OR NEW.created_at      IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'crm_message: these fields are immutable and one of them changed — id, merchant_id, customer_id, sent_to_address, channel, source_kind, source_id, purpose_key, template_id, variables, dedupe_key, created_at (and binding_id may be set once, never changed)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
