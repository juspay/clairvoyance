-- Access model v2, step 1 (2026-07-17): first-class resellers + explicit
-- access grants.
--
-- Today an umbrella (reseller) exists only as strings: merchants.reseller_id
-- points at users.id of a role='reseller' login (7 of 8 umbrella slugs in
-- prod have no such row), and a user's access lives in two unconstrained
-- JSONB arrays on users (merchant_ids / reseller_ids, with ["*"] wildcards).
--
-- This migration adds the normalized model and backfills it from those
-- arrays. The JSONB columns REMAIN AUTHORITATIVE for now — accessors
-- dual-write both representations, and the read cutover ships separately —
-- so applying this migration changes no runtime behavior by itself.
--
--   resellers             umbrella entity; id = slug (equals users.id when a
--                         reseller login exists, but a login is optional)
--   user_reseller_access  umbrella grants; all_workspaces=true is the
--                         explicit form of the old merchant_ids=["*"]
--                         wildcard (every workspace under the umbrella,
--                         present and future)
--   user_merchant_access  explicit per-workspace membership rows
--
-- Wildcards with NO umbrella linkage (admin-style rows) intentionally
-- produce no grant rows: global access is what role='admin' means.
-- Array entries pointing at nonexistent merchants/resellers are dropped
-- from the normalized projection (the arrays keep them until cutover).

BEGIN;

-- ============================================================================
-- 1. resellers — the umbrella becomes a real entity
-- ============================================================================
CREATE TABLE IF NOT EXISTS resellers (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255),
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    CONSTRAINT resellers_id_no_spaces CHECK (id !~ '\s')
);

CREATE INDEX IF NOT EXISTS idx_resellers_is_active ON resellers(is_active);

COMMENT ON TABLE resellers IS
    'Umbrella entities (workspaces group under a reseller). id is the slug '
    'used by merchants.reseller_id; a users row with the same id is an '
    'optional login for the umbrella, not the umbrella itself.';

-- Backfill order matters: reseller logins first (they carry a display name),
-- then slugs known only from merchants rows, then slugs that appear only
-- inside users.reseller_ids arrays.
INSERT INTO resellers (id, name)
SELECT id, username FROM users WHERE role = 'reseller'
ON CONFLICT (id) DO NOTHING;

INSERT INTO resellers (id, name)
SELECT DISTINCT reseller_id, reseller_id
FROM merchants
WHERE reseller_id IS NOT NULL
ON CONFLICT (id) DO NOTHING;

INSERT INTO resellers (id, name)
SELECT DISTINCT rid, rid
FROM users
CROSS JOIN LATERAL jsonb_array_elements_text(reseller_ids) AS rid
WHERE rid <> '*'
ON CONFLICT (id) DO NOTHING;

-- The self-serve umbrella (BB_SELF_SIGNUP_RESELLER_ID default) must exist
-- before the merchants FK lands, and signup writes against it.
INSERT INTO resellers (id, name, description)
VALUES (
    'breeze-self-serve',
    'Breeze self-serve',
    'Umbrella for self-registered merchants (see BB_SELF_SIGNUP_RESELLER_ID)'
)
ON CONFLICT (id) DO NOTHING;

-- Real FK: every umbrella slug on merchants now references a real entity.
-- NULL stays allowed (unassigned merchants). Deleting an umbrella that still
-- owns merchants is refused (NO ACTION).
ALTER TABLE merchants DROP CONSTRAINT IF EXISTS fk_merchants_reseller;
ALTER TABLE merchants
    ADD CONSTRAINT fk_merchants_reseller
    FOREIGN KEY (reseller_id) REFERENCES resellers(id);

-- ============================================================================
-- 2. user_reseller_access — umbrella grants (wildcard becomes explicit)
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_reseller_access (
    user_id VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reseller_id VARCHAR(255) NOT NULL REFERENCES resellers(id) ON DELETE CASCADE,
    all_workspaces BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    created_by VARCHAR(255),
    PRIMARY KEY (user_id, reseller_id)
);

CREATE INDEX IF NOT EXISTS idx_user_reseller_access_reseller
    ON user_reseller_access(reseller_id);

COMMENT ON COLUMN user_reseller_access.all_workspaces IS
    'true = access to every workspace under this umbrella, present and '
    'future (the explicit form of the legacy merchant_ids=["*"] wildcard); '
    'false = plain umbrella affiliation with per-workspace rows deciding '
    'workspace access.';

INSERT INTO user_reseller_access (user_id, reseller_id, all_workspaces)
SELECT u.id, rid, (u.merchant_ids ? '*')
FROM users u
CROSS JOIN LATERAL jsonb_array_elements_text(u.reseller_ids) AS rid
JOIN resellers r ON r.id = rid
WHERE rid <> '*'
ON CONFLICT (user_id, reseller_id) DO NOTHING;

-- A reseller login always holds an all-workspaces grant on its own umbrella.
INSERT INTO user_reseller_access (user_id, reseller_id, all_workspaces)
SELECT id, id, true FROM users WHERE role = 'reseller'
ON CONFLICT (user_id, reseller_id) DO UPDATE SET all_workspaces = true;

-- ============================================================================
-- 3. user_merchant_access — explicit workspace membership
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_merchant_access (
    user_id VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    merchant_id VARCHAR(255) NOT NULL
        REFERENCES merchants(merchant_id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    created_by VARCHAR(255),
    PRIMARY KEY (user_id, merchant_id)
);

CREATE INDEX IF NOT EXISTS idx_user_merchant_access_merchant
    ON user_merchant_access(merchant_id);

INSERT INTO user_merchant_access (user_id, merchant_id)
SELECT u.id, mid
FROM users u
CROSS JOIN LATERAL jsonb_array_elements_text(u.merchant_ids) AS mid
JOIN merchants m ON m.merchant_id = mid
WHERE mid <> '*'
ON CONFLICT (user_id, merchant_id) DO NOTHING;

COMMIT;

-- ────────────────────────────────────────────────────────────────────────────
-- Deploy-race note. This migration must run BEFORE the dual-write pods roll.
-- In the window between it committing and the last old pod draining, old pods
-- can still write users/merchants; those writes land in the JSONB arrays but
-- not in the grant tables. Two properties bound the risk:
--   * the migration is a single transaction — a concurrent merchant insert
--     with an unknown umbrella makes the FK addition fail and the WHOLE
--     migration rolls back cleanly (re-run it), never half-applies;
--   * every backfill statement above is idempotent (ON CONFLICT DO NOTHING).
-- If any users/merchants writes did land during the roll, re-running the
-- INSERT statements of sections 1–3 verbatim (psql, read-committed) after the
-- deploy reconciles the grant tables. No quiesce is needed at this fleet's
-- write rate.
-- ────────────────────────────────────────────────────────────────────────────
