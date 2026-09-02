# Phase 02 — Keyed-plan admission (B2)

**Kind**: fix · **PR title**: `fix(crm): admission guards scope to the enrollment key on keyed plans` · **Depends on**: 01 · **Notes**: §4 (W-4), §11 (B2), §12 (why keyed flows exist: WISMO, two parcels)

## Why
`entry.key` (canon T20 col 13, ruled 31 Aug 2026) says "one run per <field>", but the admission guards in `app/crm/outreach/enrol.py` read history per **customer**: `accessor.admission_facts(txn, merchant, workflow, customer)` counts ALL of the customer's runs. With the default `reenter: false` / `cooldown_hours: 24`, the second order of the same customer is refused with `reenter_disabled`. Reproduced: `_admission(keyed_definition, runs=1, latest=now-5m, now)` → `(False, "reenter_disabled")`.

## Design
- **Semantics** (decision): on a keyed plan, `reenter` and `cooldown` are judged per `(workflow, enrollment_key)`, not per customer. "Has this ORDER ever run" is what the author declared. Customer-level spam control across keys is the permission gate's job (phase 19), not admission's.
- `db/queries.py::admission_facts_query` gains an optional `enrollment_key` predicate: when given, `WHERE merchant_id=$1 AND workflow_id=$2 AND enrollment_key=$3` (drop the customer predicate — the key already implies the customer for keyed plans; keep `customer_id` in the WHERE too for tenancy paranoia: `AND customer_id=$4`). Keep the unkeyed builder path byte-identical for existing tests.
- `db/accessor.py::admission_facts(conn, merchant, workflow, customer, enrollment_key=None)`.
- `enrol.py::_enrol_in_txn`: pass `enrollment_key` when `definition.entry.key` is set (the key passed in is the payload value; unkeyed plans pass `customer_id` today — keep that but call the unkeyed builder).
- `plans.py::validate_definition`: no refusal, but when `entry.key` is set and `reenter` is false, append a **warning**? The validator returns problems only. Decision: do NOT warn; the semantics change makes the default correct for keyed plans (a new order id has no history → admitted).

## Red tests
- `tests/crm/test_workflow_queries.py`: `admission_facts_query("m1","wf","c1", enrollment_key="ORD-2")` contains `enrollment_key = $3`; unkeyed form unchanged.
- `tests/crm/test_workflow_admission.py`: monkeypatch `accessor.admission_facts` to return `{runs: 0, latest_entered_at: None}` when called with a key and `{runs: 1, ...}` without; assert `_enrol_in_txn` on a keyed definition with a fresh key inserts (reaches `insert_enrollment`) even with `reenter=False`.

## Acceptance
- Red tests fail on release, pass on branch; suite green; boundary clean.
- `context/reading-notes.md` §11 B2 → "fixed in phase 02"; §13 caveat about keyed plans removed.

## Out of scope
- Cross-key frequency caps (phase 19). Changing `reenter` defaults.
