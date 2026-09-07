# B/01 — `crm_handoff`: the table, the routes, and the letter a close writes

**Track B · step 1** · **Kind**: feat + migration · **PR title**: `feat(crm): crm_handoff — open, list and close a human handoff; a close writes a spine letter (enh B/01)` · **Depends on**: nothing; **coordinate with PR #963** (`feat(buddy-assist): native Human Assist data layer`) — if merged, the handoff record should live in (or link to) its task/session shape instead of a new table; read it first · **Notes**: §16.3 G6

## Design (when #963 is not the home)
- Migration `NNN_create_crm_handoff.sql` (next free number — 065 is taken; check `check_migrations.py --base origin/release`): `crm_handoff (id uuid pk, merchant_id text NOT NULL, customer_id uuid NOT NULL, enrollment_id uuid NOT NULL, node_id text NOT NULL, reason text, assignee text, status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')), outcome text, opened_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz, closed_by text)`; composite FK `(merchant_id, customer_id) → crm_customer (merchant_id, id)`; unique `(merchant_id, enrollment_id, node_id)` (idempotent on a lease retry); `TABLE_OWNERS["crm_handoff"] = "outreach"`; `docs/crm/migrations.md` entry.
- Outreach logic `outreach/handoffs.py`: `open_handoff(merchant, run, node_id, reason, assignee) -> HandoffRead` (INSERT … ON CONFLICT DO NOTHING RETURNING, then SELECT on conflict), `close_handoff(merchant, handoff_id, outcome, closed_by)` — ONE atom `_close_in_txn`: UPDATE `status='closed'` guarded by `status='open'` (409 otherwise) and, in the same atom, write the spine letter through `record.contracts.record_event(source="crm", topic="handoff.closed", external_id=<handoff id>, payload={enrollment_id, node_id, outcome, closed_by}, customer_id=<customer>)` — the letter is how the run learns (B/02 listens for it). Docstring: `ATOMIC: the close and its letter — a closed handoff nobody heard about is a stuck run`.
- Routes (`outreach/api.py`, merchant-facing via `assert_merchant_access`): `GET /crm/handoffs?merchant_id&status=open|closed&limit&offset` (the queue screen; never returns run context), `GET /crm/handoffs/{id}?merchant_id`, `POST /crm/handoffs/{id}/close {outcome}`.
- Schemas: `HandoffRead` leaf shape. Vocabulary for `outcome` is the plan's edge labels (free text here; B/02's validator pins the plan side).

## Red tests
- Insert idempotent (second open returns the same row); close is 409 when already closed; close emits exactly one letter with `external_id` = handoff id (monkeypatched `record_event`); list never includes context; migration numbering guard.

## Acceptance
- Suite green; boundary clean (outreach → record contracts only); `TABLE_OWNERS` complete.

## Decisions already made
- The human's action becomes a LETTER on the spine, so the run learns about it the same way it learns everything else. No direct handoff→run write.
