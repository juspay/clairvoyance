# Phase 01 — Outreach correctness fixes (B1, B3, B4)

**Kind**: fix · **PR title**: `fix(crm): wait_event reply without key, entry compare, draft→live 422` · **Depends on**: nothing · **Notes**: `context/reading-notes.md` §4 (W-1, W-3, W-5), §11 (B1, B3, B4)

## Why
Three reproduced bugs in `app/crm/outreach` that any plan using `wait_event`, any re-publish, or any status change can hit. All three are pure-function or single-statement fixes with red tests; no migration, no vocabulary change.

## B1 — a `wait_event` reply whose payload lacks `node.key` routes to "timeout"
- **Where**: `app/crm/outreach/entry.py::consume_attributed_event` (the `listening` loop) writes `{reply_<node>: None}` via `accessor.resume_run_on_event(...)` when `event.payload.get(node.key)` is None. `app/crm/outreach/walker.py::pick_next` then reads `None` and takes the `"timeout"` edge.
- **Repro (pure)**: `pick_next(WorkflowNode(id="ask", type="wait_event", topics=["button.reply"], key="button_id", minutes=60), [("confirm","YES"),("call","timeout")], {"reply_ask": None})` → `"call"`.
- **Fix**: in `entry.py`, when the answer is `None` do **not** call `resume_run_on_event`; log at info (`"wait_event reply ignored: key {node.key!r} missing (workflow …, event …)"`). The listening window continues; only the alarm can time it out. Keep `pick_next` unchanged (None still means timeout for the alarm path).
- **Red tests** (`tests/crm/test_workflow_entry.py`, new file): monkeypatch `accessor.resume_run_on_event`; (a) payload with the key → called with `{reply_ask: "YES"}`; (b) payload without the key → NOT called; (c) `pick_next` with `{}` still returns the timeout edge (pins the alarm semantics).

## B3 — publish compares the live entry as a raw dict
- **Where**: `app/crm/outreach/plans.py::validate_definition`: `if raw.get("entry") != live_entry`. `live_entry` is whatever dict was stored at the last publish; a draft that omits defaults (`reenter`, `cooldown_hours`, `where`, `key`) compares unequal to a live entry that spelled them out (or vice versa) → spurious "entry rule changed while runs are open".
- **Fix**: compare `WorkflowEntry.model_validate(raw["entry"]).model_dump()` with `WorkflowEntry.model_validate(live_entry).model_dump()`. Guard: if `live_entry` fails to validate (legacy row), fall back to raw compare.
- **Red test** (`tests/crm/test_workflow_plans.py`): draft `{"topic": "checkout.initiated"}` vs live `{"topic": "checkout.initiated", "where": {}, "reenter": false, "cooldown_hours": 24.0, "key": null}` with `occupied_nodes=["w"]` → `[]`. Keep the existing test that a REAL change (`key` added) is still refused.

## B4 — `POST /{id}/status {live}` on a never-published draft is a 500
- **Where**: `plans.py::set_status` → `accessor.set_workflow_status` → migration 057 `CHECK (status = 'draft' OR definition IS NOT NULL)` → `asyncpg.CheckViolationError` → 500 from `api.py::set_workflow_status_route`.
- **Fix** (logic, not DB): `set_status` reads the workflow first (`accessor.get_workflow`), and if `status == "live"` and `workflow.definition is None` raises `WorkflowValidationError(["publish a draft before going live"])`; `api.py` already maps that to 422. Also map `None` from the accessor (archived) to 404 as today. Do not catch the driver error in logic — the boundary rules forbid driver types outside `db/`; the pre-read is the fix.
- **Red test**: monkeypatch `accessor.get_workflow` to return a `Workflow` with `definition=None, status="draft"`; `await plans.set_status("m1","wf","live")` raises `WorkflowValidationError`. Also assert `set_status` never calls `set_workflow_status` in that case.

## Acceptance
- The three red tests fail on `origin/release` and pass on the branch.
- `pytest tests/crm` green, boundary checker clean, pyrefly 0.
- Update `context/reading-notes.md` §11 rows B1/B3/B4 with "fixed in phase 01" (docs in the same commit is fine).

## Decisions already made
- B1 is fixed by NOT resuming, not by a sentinel answer. A sentinel would need a new edge label vocabulary.
- B4 stays a logic-side check; no new DB constraint, no driver exception handling in logic.

## Out of scope
- B2 (phase 02), P1 (phase 03). N1 (type-string matching) — leave.
