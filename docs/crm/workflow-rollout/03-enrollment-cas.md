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
  `wake_at` as that lease, so one token serves the whole visit. The write
  keeps `attempts` and `last_error` (it is not the visit's success). A
  dispatching visit now writes twice.
- A reset landing BEFORE that write wins with nothing inserted.
- A letter landing AFTER it is keyed on the square the run now stands on —
  the call. It cannot pull the run back to the wait: the deaf-square
  refresh merges its facts and sets `wake_at = now()`, the visit's final
  write misses, and the re-visit starts on the call, re-derives the same
  uuid5 and adopts the lead. No double dial.
- **Failure after the write hands the token back.** `park_run` and
  `record_run_error` take the claimed square, its arrival stamp and the
  claimed context (`current_node = COALESCE($n, current_node)`), passed
  only when the visit stepped. The context matters: the walk already spent
  the claimed square's reply (the retry would take the timeout arrow) and
  counted any call it made (the retry would mint a fresh lead and dial
  again). Safe to restore: every write since the claim was ours, under the
  lease. A retry or a park is therefore judged from the square the plan
  put the run on: a calling window still holds a retry at night, and a
  parked run is resumed by the letters ITS square listens for. Pod death
  after the write is unchanged: the next claim starts on the call.
- **Trail.** A visit that starts on a dispatching square records
  `arrived_by = walk` for it (canon T26): only the step-onto write puts a
  token there outside a door. A handed-back failure leaves the rows that
  write flushed; the wait's next closing row overlaps them.

### Amendment 2 (10 Oct 2026) — an adopted FINISHED lead is heard, never awaited
Closes the stall amendment 1 only narrowed. When a re-visit is slower than
the call (the 10:00 wall of window-held runs, a deploy, pod death after the
step-onto write, a call that fails in seconds, or the re-visit from
`quiet-15m` on code without amendment 1), the call's `call.completed` is
recorded while the run stands on the call square. That square hears nothing
and reports skip the deaf refresh, so the report is spent; the re-visit then
adopts the FINISHED lead and armed the after-call wait for 1440 min on a
report that would never come again (9 Oct: 97 checkout runs).

The rule: a report already born is heard, not awaited.
- `nodes/call.py`: adopting a lead whose status is FINISHED returns its
  natural id (`call_id`, else the lead id) under `_finished_report`
  (popped by the walker, never persisted).
- `walker.py`: when the next square is a wait matched on THIS call's lead
  (`match.run == lead_<call>`), the walker reads the recorded report
  (`record.call_report`, the `(merchant_id, source, external_id)` dedupe
  key) and asks `entry.heard_report` — the consumer's own answer and facts
  (`_answer_for`, `_letter_facts`). The wait is not armed: its reply and
  `facts.<wait>` are written as `resume_run_by_id` would, and the walk goes
  through it in the same visit (NO_ANSWER/BUSY → `else` → gap;
  NOT_INTERESTED → `listen`).
- Not yet recorded (the mirror writes it after the ≤2 s outcome check), no
  outcome, or not decodable → the wait is armed exactly as before; the
  report, still to come, wakes it.
- Read from the stored report, not the lead row: the report carries the
  post-call eval's corrected outcome (`checked_outcome`) and the template's
  declared facts, which the lead row may not.
- A FINISHED lead never reports twice, so a late consumer pass changes
  nothing: its resume names the wait the run has left (`current_node`
  guard) and reports never take the refresh. Same uuid5 lead, so no second
  dial; the ledger counts the adopt once, as before.

### Corpus (owed)
T20 and the sealed sites in modules/05 need a "write before dispatch" trail
line; canon/06 §arrived_by gains the walk-onto case above.
