# Phase 19 — Permission-gate wiring (G1)

**Kind**: feat · **PR title**: `feat(crm): dispatch gate calls may_contact — consent, purpose, quiet hours, frequency` · **Depends on**: **PR #1021 merged** (`feat(crm): consent ledger, resolved state, and decision log`, rab1prasad) — the permission module and its `may_contact()` contract. If #1021 is not merged, stop; this phase is the call-site wiring, not the module. · **Notes**: §6 dispatch `_gate` TODO, §16.3 G1, design/gate-mechanics in the corpus


**Status (2026-09-03): deferred — revisit after #1021 merges.** #1021 is open and conflicting, its migrations are numbered 055/056 (taken; next free is 066), and its contracts expose `record_consent` and `log_decision` only — no `may_contact`. This phase is the call-site wiring of that decision, so nothing here can start until the module exposes it.
## Design
- `app/crm/connectivity/dispatch.py::_gate`: keep suppression as check #1 (fail closed, unchanged); then `decision = await may_contact(merchant_id, customer_id, channel, purpose_key, address)` from `app.crm.permission.contracts` (name per #1021). Decision shape (per canon): allow | refuse(reason) | defer(next_allowed_at). Refuse → `blocked` with the permission reason (add the words to `reasons.py`: `REASON_NO_CONSENT`, `REASON_PURPOSE_NOT_GRANTED`, `REASON_FREQUENCY_CAP`); defer → **requeue** with `next_attempt_at = next_allowed_at` (new `apply_outcome` path: status `queued`, reason `quiet_hours`, `retry_after_seconds` computed from the deadline; T16 col 22 says the gate's deferral writes `next_attempt_at`). `mint_send_token` carries `decision_id` onto the row (`crm_message.decision_id` exists, unused).
- Same wait_for deadline as the suppression probe; timeout/raise → `gate_unavailable` (fail closed).
- Frequency caps across plans: whatever #1021 exposes; if it does not, add a connectivity-side count (`messages_sent_since(merchant, customer, channel, since)`) and a per-merchant cap config as the interim — decision: implement the interim only if #1021 lacks it, and say so.

## Red tests
- `plan_for_outcome`/dispatch: deferred decision → queued row with `next_attempt_at` from the deadline and `reason=quiet_hours`; refused → blocked with the permission word; `decision_id` stamped.
- Gate still fails closed on unknown channel and on probe timeout (existing tests kept).

## Acceptance
- Suite green; boundary clean (connectivity → permission contracts only).
- Cart runbook: consent prerequisites for `marketing.*` purposes.

## Out of scope
- The consent ledger itself. Send-time optimisation.
