-- 052: crm_journey_event (A12, canon: crm.journey_event) — the journey
-- view, call arm only.
--
-- A VIEW, no new storage — "moves no data, cannot drift from its
-- sources" (canon). Reads lead_call_tracker in place (its customer_id +
-- partial index landed in 050/A15) instead of duplicating call rows
-- into a crm-owned table.
--
-- The 12-column contract is canon's, not ours: id, merchant_id,
-- customer_id, channel, direction, handled_by, started_at, ended_at,
-- outcome, recording_ref, transcript_ref, source_kind. Every future arm
-- (chat, crm.message, consent, the crm_event_raw commerce arm) projects
-- into this SAME column list via CREATE OR REPLACE VIEW ... UNION ALL —
-- so columns this arm has no data for (handled_by, transcript_ref) are
-- NULL here rather than the view growing new columns later.
--
-- id is cast to text: lead_call_tracker.id is varchar(255), and canon
-- notes every arm casts to text in the union (other arms may have uuid
-- ids). (source_kind, id) is this row's provenance pair.
--
-- Ordering is the caller's job (db/queries.py) — canon's keyset cursor
-- is (started_at, id). started_at is COALESCE(call_initiated_time,
-- created_at), an expression 050's index doesn't carry (that index is
-- (customer_id, created_at), built for an equality + created_at read,
-- not this COALESCE) — every read here pays an explicit Sort. Correct
-- today: per-customer cardinality keeps it trivial. Once pagination
-- makes this hot, the fix is an expression index on the COALESCE, not
-- reverting to created_at — backlog leads (call_initiated_time NULL)
-- still need to surface at push time, per canon's "attempted or not".
--
-- direction is lowercased on the way out: canon's vocabulary is
-- inbound | outbound, but lead_call_tracker.call_direction stores
-- OUTBOUND / INBOUND. Every future arm (message, chat, consent) emits
-- canon's lowercase directly, so this arm normalizes at the view rather
-- than carrying two vocabularies through one column.
--
-- Filter: "Rows that resolve no customer are excluded, not faked" (canon)
-- — WHERE customer_id IS NOT NULL.
--
-- Registered in TABLE_OWNERS (scripts/check_crm_boundaries.py) as
-- "record", the same as crm_event_raw, even though this is a VIEW: rule
-- 6 (map completeness) is CREATE-TABLE-driven and would never require
-- an entry here, but rule 1 (quoted-literal confinement) is name-driven
-- and works off any registered key — registering the view gives it the
-- same confinement as a real table, for free.

CREATE VIEW crm_journey_event AS
SELECT
    CAST(id AS text) AS id,
    merchant_id,
    customer_id,
    'call'::text AS channel,
    LOWER(call_direction) AS direction,
    NULL::text AS handled_by,
    COALESCE(call_initiated_time, created_at) AS started_at,
    call_end_time AS ended_at,
    outcome,
    recording_url AS recording_ref,
    NULL::text AS transcript_ref,
    'call'::text AS source_kind
FROM lead_call_tracker
WHERE customer_id IS NOT NULL;
