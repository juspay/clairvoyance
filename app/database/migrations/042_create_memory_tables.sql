-- 042_create_memory_tables.sql
-- Persistent per-user memory for Breeze Buddy (voice + chat).
-- Prerequisite: the pgvector extension must be enabled out-of-band by a
-- privileged database role before this migration runs.

CREATE TABLE IF NOT EXISTS user_memory (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id     varchar(255) NOT NULL,
    -- always the app-level merchant ID (non-empty); resolve_customer_key returns NULL if absent
    merchant_id     varchar(255) NOT NULL,
    -- canonical: real customer_id, OR provisional 'phone:<normalized>'
    customer_key    varchar(255) NOT NULL,
    -- 'customer_id' | 'phone'  (phone = provisional, mergeable via customer_identity)
    key_type        varchar(16) NOT NULL,
    fact            text NOT NULL,
    category        varchar(64),
    structured      jsonb NOT NULL DEFAULT '{}',
    embedding       halfvec(768),
    source_channel  varchar(16),
    confidence      real NOT NULL DEFAULT 1.0,
    operation_key   varchar(512),
    expires_at      timestamptz,
    -- non-null = retired by a newer contradicting fact (kept for audit)
    superseded_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- app-layer managed: all UPDATE queries set updated_at = now() explicitly; no DB trigger
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_memory_reseller_nonempty
        CHECK (length(btrim(reseller_id)) > 0),
    CONSTRAINT user_memory_merchant_nonempty
        CHECK (length(btrim(merchant_id)) > 0),
    CONSTRAINT user_memory_customer_nonempty
        CHECK (length(btrim(customer_key)) > 0),
    CONSTRAINT user_memory_fact_nonempty
        CHECK (length(btrim(fact)) > 0),
    CONSTRAINT user_memory_key_type_valid
        CHECK (key_type IN ('customer_id', 'phone')),
    CONSTRAINT user_memory_category_valid
        CHECK (
            category IS NULL
            OR category IN ('preference', 'attribute', 'outcome', 'context')
        ),
    CONSTRAINT user_memory_source_channel_valid
        CHECK (source_channel IS NULL OR source_channel IN ('voice', 'chat')),
    CONSTRAINT user_memory_confidence_valid
        CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

-- Fast lookup by identity (active facts only)
CREATE INDEX IF NOT EXISTS idx_user_memory_identity
    ON user_memory (reseller_id, merchant_id, customer_key)
    WHERE superseded_at IS NULL;

-- Makes replayed curator operations idempotent within an identity scope.
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_memory_operation_key
    ON user_memory (reseller_id, merchant_id, customer_key, operation_key)
    WHERE operation_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_user_memory_expiry
    ON user_memory (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_user_memory_embedding_hnsw
    ON user_memory
    USING hnsw (embedding halfvec_cosine_ops)
    WHERE superseded_at IS NULL;

-- customer_identity: phone <-> customer_id alias / resolver cache.
-- Populated the moment a conversation carries both pieces of identity.
-- Drives (a) fast phone->id resolution on later voice calls and
-- (b) the phone:* -> customer_id memory merge in the drain worker.
CREATE TABLE IF NOT EXISTS customer_identity (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id             varchar(255) NOT NULL,
    -- always the app-level merchant ID (non-empty); resolve_customer_key returns NULL if absent
    merchant_id             varchar(255) NOT NULL,
    phone                   varchar(32) NOT NULL,
    customer_id             varchar(255) NOT NULL,
    status                  varchar(16) NOT NULL DEFAULT 'ACTIVE',
    conflicting_customer_id varchar(255),
    conflicted_at           timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    -- app-layer managed: upsert_alias_query sets updated_at = now() explicitly; no DB trigger
    updated_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (reseller_id, merchant_id, phone),
    CONSTRAINT customer_identity_status_valid
        CHECK (status IN ('ACTIVE', 'CONFLICTED')),
    CONSTRAINT customer_identity_conflict_shape_valid
        CHECK (
            (
                status = 'ACTIVE'
                AND conflicting_customer_id IS NULL
                AND conflicted_at IS NULL
            )
            OR
            (
                status = 'CONFLICTED'
                AND conflicting_customer_id IS NOT NULL
                AND conflicted_at IS NOT NULL
            )
        )
);
