-- 032_create_memory_tables.sql
-- Persistent per-user memory for Breeze Buddy (voice + chat).
-- Requires: pgvector extension enabled on the managed Postgres instance.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS user_memory (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id    varchar(255) NOT NULL,
    -- always the app-level merchant ID (non-empty); resolve_customer_key returns NULL if absent
    merchant_id    varchar(255) NOT NULL,
    -- canonical: real customer_id, OR provisional 'phone:<normalized>'
    customer_key   varchar(255) NOT NULL,
    -- 'customer_id' | 'phone'  (phone = provisional, mergeable via customer_identity)
    key_type       varchar(16)  NOT NULL,
    fact           text NOT NULL,
    category       varchar(64),
    structured     jsonb NOT NULL DEFAULT '{}',
    embedding      vector(1536),
    source_channel varchar(16),
    confidence     real DEFAULT 1.0,
    -- non-null = retired by a newer contradicting fact (kept for audit)
    superseded_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- app-layer managed: all UPDATE queries set updated_at = now() explicitly; no DB trigger
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Fast lookup by identity (active facts only)
CREATE INDEX IF NOT EXISTS idx_user_memory_identity
    ON user_memory (reseller_id, merchant_id, customer_key)
    WHERE superseded_at IS NULL;

-- customer_identity: phone <-> customer_id alias / resolver cache.
-- Populated the moment a conversation carries both pieces of identity.
-- Drives (a) fast phone->id resolution on later voice calls and
-- (b) the phone:* -> customer_id memory merge in the drain worker.
CREATE TABLE IF NOT EXISTS customer_identity (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id   varchar(255) NOT NULL,
    -- always the app-level merchant ID (non-empty); resolve_customer_key returns NULL if absent
    merchant_id   varchar(255) NOT NULL,
    phone         varchar(32)  NOT NULL,
    customer_id   varchar(255) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- app-layer managed: upsert_alias_query sets updated_at = now() explicitly; no DB trigger
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (reseller_id, merchant_id, phone)
);
