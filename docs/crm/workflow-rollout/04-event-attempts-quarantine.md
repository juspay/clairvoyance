# Phase 04 — Event attempts + quarantine after N (P2)

**Kind**: fix + migration · **PR title**: `fix(crm): crm_event_raw counts attempts and quarantines poison rows` · **Depends on**: nothing · **Notes**: §3 (record OBSERVATION), §11 (P2)

## Why
`app/crm/record/workers.py::_pass_in_txn` claims pending rows FOR UPDATE SKIP LOCKED ordered by `received_at`; a row whose consumer raises deterministically (a bad live definition, a DB error on one merchant) stays `processed_at IS NULL` forever, is re-claimed every poll at the head of the queue, and re-runs `resolve()`/`assert_facts()` each time. Compare `crm_workflow_enrollment.attempts` (058), which the claim increments.

## Design
- **Migration `NNN_add_attempts_to_crm_event_raw.sql`** (next free number): `ALTER TABLE crm_event_raw ADD COLUMN attempts smallint NOT NULL DEFAULT 0;` and `CREATE OR REPLACE FUNCTION crm_event_raw_immutable()` so `attempts` is an allowed change (copy 051's function body; the immutability list is the ingestion fields only — `attempts` is envelope, like `processed_at`). Header comment cites T13 and this phase. Register nothing new in `TABLE_OWNERS` (no new table).
- `db/queries.py::claim_pending_events_query`: becomes `UPDATE crm_event_raw SET attempts = attempts + 1 WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING <columns incl. attempts>` — the claim spends an attempt (the same shape as `claim_due_runs_query` and `claim_queued_messages_query`). The lock is still held by the enclosing transaction (`_pass_in_txn`).
- `schemas.py::RawEvent` gains `attempts: int = 0`; `decoder.py` maps it.
- `workers.py::_pass_in_txn`: in the per-row `except`, if `event.attempts >= CRM_EVENT_MAX_ATTEMPTS` (new static config, default 5, `app/core/config/static.py` beside `CRM_WORKER_*`) → `accessor.quarantine_event(txn, id, f"consumer_error after {n} attempts: {e}")` **outside the failed savepoint** (the savepoint rolled back; the quarantine must be its own statement on `txn`, which is still valid). Otherwise log and leave pending as today. Log at error with the event id and source/topic (never payload).
- `observe_processed_event` unchanged.

## Red tests
- `tests/crm/test_event_worker.py`: (a) claim SQL contains `attempts = attempts + 1` and RETURNING `attempts`; (b) with `attempts=5` and a raising consumer, `quarantine_event` is called with a reason starting `consumer_error`; with `attempts=1` it is not.
- `tests/crm/test_check_migrations.py` untouched (numbering guard runs in CI).

## Acceptance
- Migration applies on a fresh DB (`uv run python scripts/migrate.py up` locally if a Postgres is available; otherwise state in the PR that it was not run) and `check_migrations.py --base origin/release` passes.
- Docs: `docs/crm/migrations.md` ownership table unchanged (same owner); add one line under the CRM table template notes: "envelope columns (processed_at, quarantine_reason, customer_id, attempts) are the only mutable ones".

## Decisions already made
- Quarantine, not delete. Replay is the recovery mechanism; a quarantined row is re-processable by clearing `processed_at`/`quarantine_reason` by hand.
- Attempts counted by the claim, not by the failure, so a crash mid-row counts.

## Out of scope
- An operator endpoint to re-drive quarantined rows (backlog).
