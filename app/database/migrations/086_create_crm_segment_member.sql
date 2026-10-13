-- 086: outreach — crm_segment_member (canon T21, canon/05-audiences.md:32-51).
-- Segments phase 2: a static list's members, shipped with the writer that
-- fills them (outreach/segments.py add_members).
--
-- Static membership, stored honestly — a frozen list IS its member set. Each
-- member arrives through resolve() (people are never stored in arrays), then
-- one row here. A pure child row: no merchant_id (canon/05-audiences.md:49-51)
-- — every read enters through the already-scoped parent segment, and uuid
-- keys cannot collide across merchants. The composite PK IS the set: adding
-- the same customer twice is a no-op (ON CONFLICT DO NOTHING), never a
-- duplicate.

CREATE TABLE crm_segment_member (
    segment_id   uuid NOT NULL REFERENCES crm_segment (id) ON DELETE CASCADE,
    customer_id  uuid NOT NULL REFERENCES crm_customer (id),
    added_at     timestamptz NOT NULL DEFAULT now(),
    -- Where the member came from: "import diwali-vips.csv", "console: Anjali".
    source       text,
    PRIMARY KEY (segment_id, customer_id)
);

-- Two invariants no writer may break, enforced here rather than by the
-- writer's good intentions (law 9), because a child row has no merchant_id
-- of its own to be checked against:
--   1. Members belong to STATIC lists only. A member row under a dynamic
--      segment would be silently ignored by every read.
--   2. The customer belongs to the segment's merchant. Without merchant_id on
--      the row, the tenant-pinned FK (the 049/058/073 precedent) cannot be
--      declared — so the pin is checked here instead: no row can ever put
--      merchant B's customer on merchant A's list.
CREATE OR REPLACE FUNCTION crm_segment_member_guard() RETURNS trigger AS $$
DECLARE
    segment_merchant   text;
    segment_definition jsonb;
BEGIN
    SELECT merchant_id, definition
      INTO segment_merchant, segment_definition
      FROM crm_segment
     WHERE id = NEW.segment_id;
    IF segment_definition IS NOT NULL THEN
        RAISE EXCEPTION 'crm_segment_member: segment % is dynamic — only a static list (definition NULL) holds member rows', NEW.segment_id;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM crm_customer
         WHERE id = NEW.customer_id AND merchant_id = segment_merchant
    ) THEN
        RAISE EXCEPTION 'crm_segment_member: customer % is not a customer of segment %''s merchant', NEW.customer_id, NEW.segment_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER crm_segment_member_guard
    BEFORE INSERT OR UPDATE ON crm_segment_member
    FOR EACH ROW EXECUTE FUNCTION crm_segment_member_guard();
