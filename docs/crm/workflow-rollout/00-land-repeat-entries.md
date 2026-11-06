# Phase 00 — Land repeat entries ourselves (supersede PR #1041 with its fixes folded in)

**Kind**: feat (carried) + fix · **PR title**: `feat(crm): repeat entries — on_repeat + debounce_minutes, with founding-event and debounce guards` · **Depends on**: nothing · **Notes**: §12 "PR #1041", §11 P9/P10, `context/nits.md` N18–N21 · **Wave 0 — first thing in the queue**

## Why this is a phase and not "wait for manas"
We run the whole rollout ourselves. #1041 (`manas-narra:feat/crm-repeat-entries`, one commit `0c45714`, base `75594cb`) is reviewed, CI-green, and merges cleanly onto today's `release`, but it lives on a fork branch we cannot push to, carries three unresolved CodeRabbit nits, and needs two one-line guards (P9, P10) before the cart flow can rely on it. So we land it: cherry-pick the commit onto a branch of ours, apply the five fixes in the same single commit, open our PR crediting manas, and ask for #1041 to be closed as superseded. Phases 07 and 16 depend on this being merged.

## What #1041 adds (so the reviewer of our PR knows what is carried vs new)
`entry.on_repeat` ∈ {ignore, refresh_latest, refresh_max(<field>), accumulate} and `entry.debounce_minutes`; new `app/crm/outreach/repeat.py` (`parse_repeat_policy`, `repeat_plan`, `apply_repeat`); `db/queries.py::patch_open_run_query` — one idempotent UPDATE on the run standing on `nodes[0]`, found by `enrollment_key`, marking the event in `context.repeat_event_ids`, sliding `wake_at`; hooked from `entry._try_enrol` when `enrol()` returns None; validator rules (policy word; debounce needs a wait first); `repeat_event_ids`/`repeat_items` as bookkeeping; 16 tests in `tests/crm/test_workflow_repeat.py`. 9 files, +533/−4.

## Steps
1. `git fetch origin pull/1041/head:pr-1041 && git checkout -b claude/crm-phase-00-land-repeat-entries origin/release && git cherry-pick 0c45714` (clean today; if it conflicts later in `entry.py`, the intent is: after `enrol()` returns None, call `apply_repeat` with the same args as the PR).
2. Apply the five fixes, all inside the cherry-picked commit (`git commit --amend`):
   - **P9 — founding-event redelivery**: in `patch_open_run_query` WHERE add `AND context->>'source_event_id' IS DISTINCT FROM $5::text`. A redelivered copy of the run's own founding event is refused by `source_event_used`, falls into `apply_repeat`, is not in `repeat_event_ids`, and would overwrite newer facts with the first snapshot and restart the timer.
   - **P10 — debounce only extends**: `wake_at = CASE WHEN $10::float8 > 0 THEN GREATEST(wake_at, now() + make_interval(secs => $10::float8 * 60)) ELSE wake_at END`. With `now()+N` a debounce shorter than the remaining entry wait pulls the alarm EARLIER (manas flagged it himself in the PR comment; the answer is GREATEST).
   - **N18 — non-finite refresh_max**: `repeat.py::_as_number` returns None unless `math.isfinite(value)` ("nan"/"inf" otherwise always win in Postgres ordering).
   - **N19 — accumulate on a scalar `repeat_items`**: `entry.py::_context_from_payload` skips keys in `nodes._BOOKKEEPING_KEYS` and with `nodes._BOOKKEEPING_PREFIXES` (import both; they are the one definition), so a producer payload can never plant `repeat_items`/`source_event_id`/`lead_*` in context.
   - **N20 — refreshed phone**: `_try_enrol` passes `apply_repeat` the same `context` dict it built for `enrol()` **minus `source_event_id`** (P9 needs the founding id to stay put), so a changed phone reaches the run.
3. Add the tests (N21): SQL contains the P9 predicate and `GREATEST(wake_at`; `_as_number("nan"/"inf"/"-inf")` → None; bookkeeping keys never enter context; monkeypatched `apply_repeat` receives `phone` and not `source_event_id`. Keep all 16 of manas's tests.
4. Commit message keeps manas's original body, adds a "Carried from #1041 by @manas-narra; adds P9/P10/N18–N20" paragraph and `Co-authored-by: manas-narra <…>` (take the email from `git log pr-1041 -1 --format=%ae`).
5. Full check list from `README.md`; open the PR; in the description link #1041 and ask a maintainer to close it as superseded. Do not comment on #1041 yourself beyond that link.

## Acceptance
- `pytest tests/crm` green with the 16 carried + 5 new tests; boundary checker clean; pyrefly 0; one commit.
- After merge: `context/reading-notes.md` §12 "Two asks on the PR" and §11 P9/P10 are done (no edit needed; the PR list is the ledger).

## Decisions already made
- Supersede, do not wait. Attribution stays with manas in the commit and PR body.
- `GREATEST` semantics for debounce. `pin`-vs-`migrate` is irrelevant here (phase 11).

## Out of scope
- Generalising repeats beyond the entry node (phase 16). Manas's open questions on list-shaped facts / `accumulate(<field>)` (backlog G4).
