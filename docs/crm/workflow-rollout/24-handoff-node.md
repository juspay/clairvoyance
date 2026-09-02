# Phase 24 — `handoff` node: hand a run to a human and wait for them (G6)

**Kind**: feat + migration · **PR title**: `feat(crm): handoff node and crm_handoff — a human closes it, the run continues` · **Depends on**: 15, 18; **coordinate with PR #963** (`feat(buddy-assist): native Human Assist data layer`) — if merged, the handoff record should live in (or link to) its task/session shape instead of a new table; read it first. · **Notes**: §16.3 G6 · **Wave 6**

## Design (when #963 is not the home)
- Migration `NNN_create_crm_handoff.sql`: `crm_handoff (id uuid pk, merchant_id text NOT NULL, customer_id uuid NOT NULL, enrollment_id uuid NOT NULL, node_id text NOT NULL, reason text, assignee text, status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')), outcome text, opened_at, closed_at, closed_by)`; composite FK to `crm_customer (merchant_id, id)`; unique `(merchant_id, enrollment_id, node_id)` (idempotent on lease retry); `TABLE_OWNERS["crm_handoff"] = "outreach"`.
- Node: `{id, type: "handoff", reason: str, assignee?: str, minutes: int}` — `is_wait: True`, `listens: True`: opening the handoff is the arrival action (insert row, ON CONFLICT DO NOTHING), then the run waits for `handoff.closed` OR the alarm. Closing: `POST /crm/handoffs/{id}/close {outcome}` (admin/merchant via `assert_merchant_access`) updates the row AND writes a spine letter `handoff.closed` through `record.contracts.record_event` (source `crm`, external_id = handoff id, payload `{enrollment_id, node_id, outcome}`) — the run's `wait_event` machinery (phase 18's `match` on `enrollment_id`) resumes it with `reply_<node> = outcome`; `timeout` edge for "nobody picked it up".
- Reads: `GET /crm/handoffs?merchant_id&status=open` for the queue screen.

## Red tests
- Insert idempotent; close emits the letter with the right external_id; walker: `handoff → {resolved | escalate | timeout}` edges chosen by outcome/alarm; a closed handoff cannot be closed twice (409).

## Decisions already made
- The human's action becomes a LETTER on the spine, so the run learns about it the same way it learns everything else. No direct handoff→run write.
