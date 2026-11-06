-- 068: crm_event_schema (canon T24, sealed 1 Sep 2026) — the REGISTERED
-- contract for a push vendor's events (design/event-catalog.md §Vendor
-- events). One row per (merchant, source, topic); the fields document is
-- the whole registration; the row is cold by design.
--
-- Numbering trail: sealed as 060 on 1 Sep; #1037 took 060, #1050 took 061
-- (crm_channel_template) and the workflow rollout took 062-064, so this is
-- 068 — the next free number at implementation time (docs/crm/migrations.md).
--
-- Deliberate shape (canon T24 notes):
--   * fields jsonb is ONE document [{path, type, label, keyable, variable,
--     values[], identity, deprecated}], never per-field rows. The type
--     vocabulary lives in the REGISTRATION VALIDATOR (record/catalog.py),
--     never a CHECK — the 027 scar. `identity` is a ROLE (phone | name), at
--     most one field per role: the vendor keeps THEIR names (rider_phone)
--     and the mapping tells decode where the handle lives.
--   * status IS a CHECK (closed lifecycle, like T19/T20): detected = the
--     event worker's discovery upsert saw an unregistered topic (fields
--     empty — the nudge row); registered = a human/vendor signed the
--     schema. Registration upgrades the same row.
--   * version bumps on every re-registration — the audit stamp (T19's
--     precedent); removals are deprecations inside `fields`, never deletes.
--   * first_seen_at is written ONCE by discovery. No per-event counters
--     here — "312 this week" is computed on read from crm_event_raw.
--   * UNIQUE (merchant_id, source, topic): merchant-first per tenancy law.
--
-- Readers: the catalog API merges these rows with the code CATALOG; the
-- publish validator receives the merged catalog as an argument. The FLOW
-- runtime (entry evaluator, walker) never reads this table; the DECODE
-- step reads only the cached identity mapping.

CREATE TABLE crm_event_schema (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id   text NOT NULL,
    source        text NOT NULL,
    topic         text NOT NULL,
    label         text,
    fields        jsonb NOT NULL DEFAULT '[]'::jsonb,
    status        text NOT NULL DEFAULT 'detected'
                  CHECK (status IN ('detected', 'registered')),
    version       integer NOT NULL DEFAULT 0,
    registered_by text,
    first_seen_at timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX crm_event_schema_topic_uq
    ON crm_event_schema (merchant_id, source, topic);

CREATE TRIGGER crm_event_schema_touch
    BEFORE UPDATE ON crm_event_schema
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();
