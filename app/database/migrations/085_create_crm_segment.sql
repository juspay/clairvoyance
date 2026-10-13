-- 085: outreach — crm_segment (canon T15, canon/05-audiences.md). Segments
-- phase 1: the segment row itself. Static-list members (crm_segment_member,
-- T21) arrive in phase 2 with the writer that fills them; the definition
-- compiler (dynamic segments) in phase 4.
--
-- A segment is a stored question; membership is computed at read time,
-- never stored. A static list is the one exception: an imported set IS its
-- members (canon/05-audiences.md:3-4). One row here is EITHER a dynamic
-- segment (definition set) OR a static list (definition NULL) — the kind is
-- a predicate over definition, never a stored column (law 10).
--
-- member_count + computed_at are the honesty pair (T15 cols 7-8): a static
-- list's count is exact, stamped whenever its members change; a dynamic
-- segment's will be a display-only cache. Nothing ever decides on it.

CREATE TABLE crm_segment (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id   text NOT NULL,
    -- Unique per merchant (index below); a blank name is not a name, and
    -- a stored name never starts or ends with whitespace — so the plain
    -- unique index already treats "Diwali VIPs" and "Diwali VIPs " as one
    -- name, for every writer, not only the ones that trim first.
    name          text NOT NULL CHECK (name <> '' AND name !~ '^\s|\s$'),
    -- The stored question — dynamic segments only; NULL for a static list.
    -- Always a JSON object when set: the compiler reads keys, never a bare
    -- array or scalar.
    definition    jsonb CHECK (definition IS NULL OR jsonb_typeof(definition) = 'object'),
    member_count  integer CHECK (member_count >= 0),
    computed_at   timestamptz,
    -- Who built the thing that will one day address 8,400 people.
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- T15 col 3: the name is unique per merchant — merchant_id first (tenancy law).
CREATE UNIQUE INDEX crm_segment_merchant_name_uq
    ON crm_segment (merchant_id, name);

-- The pin target for tenant-pinned FKs into this table (the 049/058/073
-- precedent): crm_broadcast.segment_id (T17 col 6) will reference
-- (merchant_id, id), so a broadcast can never freeze another merchant's
-- audience.
CREATE UNIQUE INDEX crm_segment_tenant_pin_ux
    ON crm_segment (merchant_id, id);

-- The list read's cursor (newest first per merchant), carried by the index.
CREATE INDEX crm_segment_merchant_created_ix
    ON crm_segment (merchant_id, created_at DESC, id DESC);

CREATE TRIGGER crm_segment_touch
    BEFORE UPDATE ON crm_segment
    FOR EACH ROW EXECUTE FUNCTION crm_touch_updated_at();
