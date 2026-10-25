-- 087: token_registry — revocation and last-seen (token registry 2/4).
--
-- Every long-lived token that reaches a verifier is now looked up here by its
-- hash (rbac_token.authenticate). A token the registry has never seen is
-- registered as source = 'backfilled' and let through (observe mode — that is
-- how we learn which tokens are in use, and by whom). A revoked token is
-- refused.
--
-- Revoked is the predicate `revoked_at IS NOT NULL AND revoked_at <= now()`,
-- never a stored status. The three revoke columns are written together by
-- one statement (POST /auth/tokens/{id}/revoke), and the CHECK keeps them so.
--
-- last_seen_* is bookkeeping for the reconcile before enforcement: written at
-- most once every few minutes per token, never on the request's critical
-- decision. last_seen_from is the X-Forwarded-For chain as received (or the
-- direct peer when absent) — every hop, because on the GCP load balancer the
-- caller is the FIRST entry and the balancer the last; it is caller-supplied
-- and only ever read by people, never by an access decision.

ALTER TABLE token_registry
    ADD COLUMN IF NOT EXISTS revoked_at     timestamptz,
    ADD COLUMN IF NOT EXISTS revoked_by     text,
    ADD COLUMN IF NOT EXISTS revoke_reason  text,
    ADD COLUMN IF NOT EXISTS last_seen_at   timestamptz,
    ADD COLUMN IF NOT EXISTS last_seen_from text,
    ADD COLUMN IF NOT EXISTS last_route     text;

ALTER TABLE token_registry DROP CONSTRAINT IF EXISTS token_registry_revoke_fields_together;
ALTER TABLE token_registry ADD CONSTRAINT token_registry_revoke_fields_together
    CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL
            AND revoke_reason IS NOT NULL AND btrim(revoke_reason) <> '')
    );
