-- 048: platform_identity (canon T02, B2) — the handles book.
--
-- One row per identifier ACROSS merchants: suppression + registry.
-- Written only through app/crm/platform contracts (ensure_identities,
-- record_suppression); read by the gate as one boolean probe on the
-- (kind, value) unique — the only index the send path uses.
-- No name/profile columns, ever: the platform layer only says yes or no.
--
-- The format CHECKs are compliance-critical in the DANGEROUS direction:
-- a suppression stored unnormalized while the gate probes the normalized
-- form is a MISS, and a person who said "never contact me" gets contacted.
-- With one DB role, the table defends itself (ADR 0001 amendment).

CREATE TABLE platform_identity (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL CHECK (kind IN ('phone', 'email', 'device')),
    value           text NOT NULL,
    is_suppressed   boolean NOT NULL DEFAULT false,
    suppressions    jsonb NOT NULL DEFAULT '{}'::jsonb,
    suppression_log jsonb NOT NULL DEFAULT '[]'::jsonb,
    first_seen_at   timestamptz,
    last_seen_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, value),
    CONSTRAINT platform_identity_phone_e164
        CHECK (kind <> 'phone' OR value ~ '^\+[1-9][0-9]{6,14}$'),
    CONSTRAINT platform_identity_email_lower
        CHECK (kind <> 'email' OR value = lower(value))
);

-- Shared touch trigger for every crm/platform table with updated_at.
CREATE OR REPLACE FUNCTION crm_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER platform_identity_touch
    BEFORE UPDATE ON platform_identity
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();

-- is_suppressed is DERIVED, liveness-aware, and cannot drift from the
-- jsonb no matter who writes: an entry is live when it has no "until"
-- or its "until" is in the future (expiry-as-predicate; a timed
-- suppression that lapses is flipped by the B-pod expiry sweep re-write).
CREATE OR REPLACE FUNCTION platform_identity_recompute() RETURNS trigger AS $$
DECLARE
    entry jsonb;
BEGIN
    NEW.is_suppressed := false;
    FOR entry IN SELECT value FROM jsonb_each(NEW.suppressions) LOOP
        IF entry->>'until' IS NULL
           OR (entry->>'until')::timestamptz > now() THEN
            NEW.is_suppressed := true;
            EXIT;
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Fires on EVERY insert/update, not just changes to suppressions: a direct
-- UPDATE ... SET is_suppressed = false is overwritten by recomputation —
-- the boolean is derived, and derived means no caller can set it.
CREATE TRIGGER platform_identity_recompute_suppressed
    BEFORE INSERT OR UPDATE ON platform_identity
    FOR EACH ROW EXECUTE FUNCTION platform_identity_recompute();

-- suppression_log is append-only: nobody rewrites "please stop
-- contacting me". The old array must be a strict prefix of the new one.
CREATE OR REPLACE FUNCTION platform_identity_log_append_only() RETURNS trigger AS $$
DECLARE
    old_len int := jsonb_array_length(OLD.suppression_log);
    i int;
BEGIN
    IF jsonb_array_length(NEW.suppression_log) < old_len THEN
        RAISE EXCEPTION 'suppression_log is append-only (shrink refused)';
    END IF;
    FOR i IN 0..old_len - 1 LOOP
        IF NEW.suppression_log->i IS DISTINCT FROM OLD.suppression_log->i THEN
            RAISE EXCEPTION 'suppression_log is append-only (rewrite refused)';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER platform_identity_log_guard
    BEFORE UPDATE OF suppression_log ON platform_identity
    FOR EACH ROW EXECUTE FUNCTION platform_identity_log_append_only();
