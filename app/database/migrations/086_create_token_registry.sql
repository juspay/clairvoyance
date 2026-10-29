-- 086: token_registry — one row per long-lived RBAC token we hand out.
--
-- Today a token is checked by its signature and expiry alone
-- (rbac_token.verify_rbac_token): nothing in the database knows it exists,
-- so it can never be revoked, and deactivating its owner changes nothing.
-- This table is the first step: the two long-lived mints (POST
-- /auth/s2s/token and the per-merchant token on POST /merchant) write a row
-- here, and the token carries the row's id as its `jti` claim. Nothing reads
-- the table yet — verification is unchanged.
--
-- The token itself is NEVER stored: `token_hash` is the hex sha256 of the
-- token string, which is enough to recognise a presented token and useless
-- to anyone who reads the row. Tokens minted before this table have no jti;
-- they are registered later by hash alone (source = 'backfilled'), which is
-- why the hash, not the jti, is the unique key.
--
-- `kind` and `source` vocabularies live in code (TokenKind / TokenSource in
-- app/schemas/breeze_buddy/token_registry.py); the CHECKs are on FORMAT.
-- Revoked / expired are predicates over timestamps added with their writers,
-- never a stored status.

CREATE TABLE IF NOT EXISTS token_registry (
    -- The token's `jti` claim for minted tokens; generated for backfilled
    -- tokens, which carry no jti.
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash     text NOT NULL,
    -- 'admin' (POST /auth/s2s/token) or 'merchant' (per-merchant token).
    kind           text NOT NULL,
    -- 'minted' (written at mint) or 'backfilled' (registered from use).
    source         text NOT NULL,
    -- The token's identity and scope claims, as minted.
    subject        text NOT NULL,
    username       text NOT NULL,
    role           text NOT NULL,
    reseller_ids   text[] NOT NULL,
    merchant_ids   text[] NOT NULL,
    -- The user id that asked for the mint; NULL when unknown (backfilled).
    created_by     text,
    -- The token's own iat / exp claims.
    issued_at      timestamptz NOT NULL,
    expires_at     timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT token_registry_token_hash_unique UNIQUE (token_hash),
    CONSTRAINT token_registry_token_hash_is_sha256_hex
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT token_registry_expires_after_issued
        CHECK (expires_at > issued_at)
);
