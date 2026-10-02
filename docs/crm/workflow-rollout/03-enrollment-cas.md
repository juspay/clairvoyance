# Phase 03 — Compare-and-set on enrollment writes (P1)

**Kind**: fix · **PR title**: `fix(crm): walker writes are conditional on the lease they were claimed under` · **Depends on**: 01 · **Notes**: §4 (W-2), §11 (P1), §12 (#1041 adds a second writer), §14.7

## Why
`db/queries.py::advance_run_query` (and `exit_run_query`, `park_run_query`, `record_run_error_query`) update `context`/`wake_at` unconditionally (`WHERE id=$1 AND status='waiting'`). Meanwhile `resume_run_on_event_query` (W5 replies) and PR #1041's `patch_open_run_query` (repeats) also write `context` + `wake_at` from the event worker. If a reply lands while the walker is mid-visit on that node's timeout path, the walker's `advance_run` overwrites the reply and the timeout branch wins. Same for a repeat patch. No migration needed.

## Design — the leased `wake_at` is the generation token
- `claim_due_runs_query` already sets `wake_at = now() + lease` and RETURNS the row; the decoded `EnrollmentRun.wake_at` is therefore the claim's token. Every event-side writer sets `wake_at` to something else (`now()` for replies, `now()+debounce` for repeats), and the claim itself moves it. So `AND wake_at = $leased` on the walker's writes is a correct CAS without a new column.
- Change the four walker-side builders to take `leased_wake_at: datetime` and add `AND wake_at = $n`. `exit_run` from the ENTRY consumer (`cancel_open_runs`) stays unconditional (it is the event side; goals win).
- `walker.py`: thread `run.wake_at` into `accessor.advance_run/exit_run/park_run/record_run_error`. On a CAS miss (`UPDATE … RETURNING id` → None): log info `"walker: run {id} changed under the lease — deferring to the next wake"` and return. The lease already re-arms the run; on the next claim the walker re-reads the run WITH the reply/patch and takes the right branch. Action nodes are idempotent (dedupe `run:node`, uuid5 lead), so a re-executed visit is safe (the same guarantee the lease relies on today).
- Accessors return `bool` (row matched) for these four.
- Docstrings: state the law in each builder ("the lease is the generation; a write under a stale lease is a no-op").

## Red tests
- `tests/crm/test_workflow_queries.py`: each of the four builders contains `wake_at = $` and carries the leased value in params.
- `tests/crm/test_workflow_walker.py` (new): monkeypatch accessor; `advance_run` returns False → `_advance` returns without raising and without calling `exit_run`; with True → behaves as today.

## Acceptance
- Suite green; boundary clean (no driver types in logic).
- §11 P1 → "fixed in phase 03". Note in `db/queries.py` module docstring: "walker writes are CAS on the leased wake_at; event-side writes are not".

## Decisions already made
- No `revision` column. The leased `wake_at` is sufficient and avoids a migration. Revisit only if a writer ever needs to leave `wake_at` untouched (none does).

## Out of scope
- #1041's patch query (it is event-side; unchanged). Phase 16 generalises repeats.

## Amendment (1 Oct 2026) — write the token onto a square that reaches out before executing it

A reset landing between a call square's insert and the visit's write left the
run on `quiet-15m`, the call reporting to nobody, and the re-done visit
adopting the finished lead (same uuid5) and waiting a full alarm.

- `NodeSpec.reaches_out` (call, send, action): before executing one, the
  walker `advance_run`s the token onto it under the claim's lease, leaving
  `wake_at` as that lease, so one token serves the whole visit.
  Event-side writes are keyed on the square a run stands on, so a reset
  landing before the write wins with nothing inserted, and one landing after
  cannot move the run. A dispatching visit now writes twice.
