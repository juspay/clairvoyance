# C/03 — ADR 0023: spine retention and the partitioning decision (docs)

**Track C · step 3** · **Kind**: docs (ADR) · **PR title**: `docs(crm): ADR — crm_event_raw retention and the partitioning decision (enh C/03)` · **Depends on**: C/01; **Swaroop signs the ADR off before C/04 starts** (the agent asks, does not assume) · **Notes**: migration 051 header, 056 header, §16.3

## Why
051 deferred monthly RANGE partitioning because Postgres requires the partition key inside every unique constraint and that breaks the dedupe unique `(merchant_id, source, external_id)`. At pilot volume that is right. Before a promo day it is not: the pending index and the customer index grow without bound and nothing ages rows out.

## Deliverable — ADR 0023 (`docs/crm/adr/0023-spine-retention.md`)
Options, with the recommendation: (1) keep one table, add a retention sweep on `received_at` for PROCESSED rows older than N days (the goal re-check reads `crm_event_raw` for `since = entered_event_at`, so N must exceed `exits.max_age_days` of every live plan — enforce with a validator rule against the config); (2) partition by month with the dedupe unique widened to `(merchant_id, source, external_id, received_at)` — which changes dedupe semantics across months (a redelivery 31 days later is a new row); (3) partition with a separate small `crm_event_dedupe (merchant_id, source, external_id)` table holding the unique, written in the same statement via CTE. **Recommend (1) now, (3) when volume demands** — (2) is rejected because it silently weakens the front-door law.

## Acceptance
- ADR merged with Swaroop's sign-off recorded in the PR; `docs/crm/migrations.md` notes the retention rule. No code.
