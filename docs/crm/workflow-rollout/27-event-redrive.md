# Phase 27 — Re-drive quarantined events (phase 04 follow-up)

**Kind**: feat · **PR title**: `feat(crm): list and re-drive quarantined spine events` · **Depends on**: 04 · **Notes**: §3 record, phase 04 "Out of scope" · **Wave 7**

## Design
- `GET /crm/ingest/events?merchant_id&quarantined=true&limit&before_id` (admin): the quarantine queue — id, source, topic, external_id, received_at, attempts, quarantine_reason. Never the payload in the list (PII); `GET /crm/ingest/events/{id}?merchant_id` returns it for one row.
- `POST /crm/ingest/events/{id}/redrive?merchant_id` (admin): one UPDATE `SET processed_at = NULL, quarantine_reason = NULL, attempts = 0 WHERE merchant_id=$1 AND id=$2 AND quarantine_reason IS NOT NULL RETURNING id` — the immutability trigger (051 + phase 04 amendment) permits these three columns. 404 if not quarantined. The next worker pass picks it up in `received_at` order.
- `POST /crm/ingest/events/redrive?merchant_id&reason_prefix=consumer_error` — batched (LIMIT 500) for the "we fixed the consumer" case; returns the count.
- Queries in `record/db/queries.py`; logic in `record/ingest.py` (`redrive_event`, `redrive_events`); routes on `record/api.py::ingest_router`.

## Red tests
- Query shapes (merchant-first, the three columns only); 404 path; batch limit.

## Decisions already made
- Re-drive resets attempts. Payload never appears in a list response.
