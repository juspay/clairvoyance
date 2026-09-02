# Phase 35 — Scale hardening: spine partitioning ADR and retention

**Kind**: docs (ADR) + migration · **PR title**: `feat(crm): crm_event_raw retention and the partitioning decision` · **Depends on**: 27; **needs an ADR signed off by Swaroop before the migration half starts** · **Notes**: migration 051 header (deliberate non-partitioning), 056 header, §16.3 · **Wave 7, last**

## Why
051 deferred monthly RANGE partitioning because Postgres requires the partition key inside every unique constraint and that breaks the dedupe unique `(merchant_id, source, external_id)`. At pilot volume that is right. Before a promo day it is not: the pending index and the customer index grow without bound and nothing ages rows out.

## Part A — ADR 0023 (docs)
Options, with the recommendation: (1) keep one table, add a retention sweep on `received_at` for PROCESSED rows older than N days (the goal re-check reads `crm_event_raw` for `since = entered_event_at`, so N must exceed `exits.max_age_days` of every live plan — enforce with a validator rule against the config); (2) partition by month with the dedupe unique widened to `(merchant_id, source, external_id, received_at)` — which changes dedupe semantics across months (a redelivery 31 days later is a new row); (3) partition with a separate small `crm_event_dedupe (merchant_id, source, external_id)` table holding the unique, written in the same statement via CTE. **Recommend (1) now, (3) when volume demands** — (2) is rejected because it silently weakens the front-door law.

## Part B — retention (after sign-off)
- Static config `CRM_EVENT_RETENTION_DAYS` (default 180); sweep on the event-worker pod's housekeeping (like `run_retention_sweep_tick`): `DELETE … WHERE processed_at IS NOT NULL AND received_at < cutoff` batched; never quarantined rows (they are pending work); never rows referenced as `source_event_id` by a non-exited run (join). Validator: `exits.max_age_days <= CRM_EVENT_RETENTION_DAYS`.
- Journey view (055) is unaffected (it reads `lead_call_tracker`).

## Red tests
- Sweep SQL excludes quarantined and referenced rows; validator refuses a plan whose max age exceeds retention.
