-- 088: token_registry.replaced_by — rotation (token registry 3/4).
--
-- POST /auth/tokens/{id}/rotate mints a successor and, in the same
-- statement, points the old row at it and dates the old row's revocation
-- `now() + grace`. Until then both tokens are valid (revoked is the
-- predicate `revoked_at <= now()`), so a service can switch without a gap.
--
-- A replaced row is always a revoked-or-revoking row: the CHECK keeps
-- replaced_by from existing without revoked_at (087's CHECK already keeps
-- revoked_at, revoked_by and revoke_reason together).

ALTER TABLE token_registry
    ADD COLUMN IF NOT EXISTS replaced_by uuid REFERENCES token_registry (id);

ALTER TABLE token_registry DROP CONSTRAINT IF EXISTS token_registry_replaced_is_revoked;
ALTER TABLE token_registry ADD CONSTRAINT token_registry_replaced_is_revoked
    CHECK (replaced_by IS NULL OR revoked_at IS NOT NULL);
