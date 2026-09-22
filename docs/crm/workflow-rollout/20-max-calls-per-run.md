# Phase 20 — A daily ceiling on the calls one run may place (max_calls_per_day)

**Kind**: feat · **PR title**: `feat(crm): cap max calls per run per day` · **Depends on**: 18 (merged) · **Notes**: `nodes/call.py` (967a86df: each visit mints its own lead), `walker.py::_advance` (where a square's outcome word is recorded), canon T19 `exits` / T26 `outcome`, `schemas.WaitWindow` (the clock rule this follows), `outreach/window.py` (the concern-file shape `ceiling.py` copies), `predicates.py` (the `customer.` source the `run.` source is built like), `modules/05-outreach` §gate (the ruling in *Decisions* §1)

## Why now

Since 967a86df a call square mints a fresh lead on **every visit**
(`lead_visits_<node>` keys the id). An arrow back to a call square is therefore
a second call, a third, … with nothing bounding it but `exits.max_age_days` and
the walker's `_MAX_STEPS_PER_VISIT` (ten immediate squares — ten leads in one
visit for a `call → condition → call` cycle with no wait between). No shipped
board draws such an arrow today (the fallback board goes
`after-call → wa-fallback → wait-1d → end`; the ladder only moves to LATER
stages), so this is the guardrail for the first one that does — together with
the publish law (§7) that refuses the no-wait loop outright, since a run parked
as a runaway never commits the ledger the ceiling counts from.

The cart runbook and `18-outcome-feedback.md` both still claimed an arrow back
"re-issues the same lead id — no second call", which stopped being true on
10 Sep. Both are corrected in this phase.

## Two retry layers — this phase caps ONE of them

| Layer | Who decides | Counter | Bound |
|---|---|---|---|
| Buddy re-dial (NO_ANSWER → dial again) | `call_execution_config.max_retry` + `retry_offset`, per template | `lead_call_tracker.attempt_count` (0-based, retries only); each re-dial is its own row, `enrollment_id IS NULL`, same `request_id` | already bounded, per lead |
| Walker revisit (the run lands on a call square again) | the plan's board | the run's day ledger, `calls_today` | **unbounded today — this phase** |

They compose multiplicatively: `max_calls_per_day: 3` with a template whose
`max_retry: 2` is up to nine rings in a day. The runbook says so in one line;
the plan does not reach into buddy's counter (Decisions §3).

## Design

**The rule in one sentence: past the day's ceiling, a call square places no call
and the run takes its normal arrow; the count starts over at midnight on the
plan's clock.** (Skip-and-walk-on decided 22 Sep 2026, per-day 22 Sep 2026; the
alternative endings are recorded under Decisions §4.)

1. **The words.** On `WorkflowExits`, beside `max_age_days`:
   - `max_calls_per_day: Optional[int] = Field(None, ge=1)` — calls one run may
     place in a calendar day, over every call square and every revisit.
   - `timezone: Optional[str] = None` — the clock the day boundary is read on,
     validated with `ZoneInfo` on the model, and REQUIRED whenever a ceiling is
     named. The `WaitWindow` reasoning applies: 23:30 IST on the 22nd is still
     the 22nd, and a server-clock day would hand the run a fresh allowance at
     18:30 local.

   **Neither is defaulted on the MODEL, and the default is given at WRITE
   instead** — `plans.with_default_ceiling`, applied by `create_workflow` and
   `draft_version`, stamps `6` / `Asia/Kolkata` into the document itself.
   Every plan written from now on still gets a ceiling, but the guarantee ADR
   0023 sells stays intact: `definitions.py` re-validates the STORED T25
   document on every claim, so a pydantic default would hand a ceiling to
   documents whose authors never wrote one, and §5's audit property — the
   immutable row answering *"what did this run execute"* — would start
   depending on which code version read the row. Nor is the bend safe in one
   direction: a capped call square places **no lead**, so a board whose next
   square listens for `call.completed` hears nothing and leaves by its timeout
   arrow. That is a change of *meaning* for a run already walking, and which
   arrow it is belongs to the merchant's board.

   Stamping at write also makes the number visible where the author can read
   it. `max_calls_per_day: null` in the document is the explicit opt-out; the
   clock is stamped alongside the ceiling, and a ceiling with an explicit
   `null` clock is still refused — the day has to be counted on something.

   The stamp is only GIVEN to a board that has a call square, because §7's law
   refuses a ceiling no square can reach. A ceiling the author typed gets its
   clock either way, so a call-less board naming one is refused by that law
   rather than for a missing clock.

2. **The ledger, and what it is NOT.** The run carries one day-stamped record
   in its context:

   ```json
   "calls_today": { "day": "2026-09-22", "n": 3 }
   ```

   A record stamped with any other day reads as 0 — **that is the reset.** No
   sweep, no cron, no write at the boundary: the first call of the new day
   re-stamps the record rather than adding to it.

   It is deliberately a **separate key from `lead_visits_*`**. Those counters
   key the lead ids (`uuid5(run:node:visit)`) and must stay monotonic for the
   run's life. Rolling them back at midnight would re-derive an id the table
   already holds, the primary key would absorb the insert as a lease retry, and
   the first call of the new day would silently never be placed — the 967a86df
   scar arrived at from the other direction. A test is named for it.

3. **The check, in the call node, before anything is read or written.**
   `execute` asks `max_calls_reached(context, exits)`; at or above the ceiling
   it returns at once — no `get_template_by_id`, no config read, no
   `blocks_for`, no insert, no ledger write — with exactly `_outcome:
   "max_calls"` for the trail row (below), and **nothing else**.

   Idempotent by construction: the same visit re-run under a lost lease yields
   the identical patch and touches nothing.

   On a call that IS placed the node writes `calls_today` forward, and that is
   the whole write. There is no second key recording whether the ceiling is
   reached, so nothing can disagree with the count and nothing has to be
   cleared.

   **`max_calls_reached` is COMPUTED, never stored** (law 10: no stored derived
   state a predicate can answer). `outreach/ceiling.py` owns the ledger key,
   the predicate and the day arithmetic — a concern file beside `window.py`,
   for the same reason: plan-level scheduling arithmetic that is pure and has
   more than one caller.

   **A plan names it as `run.max_calls_reached`**, the grammar's fifth field
   source (`predicates.RUN_PREFIX` / `RUN_FACTS`), built exactly like
   `customer.<column>`: a prefix, a CLOSED list of names, one place the value
   comes from, and a refusal at publish for a name that is not on the list. No
   square computes it and no square names it in code — `nodes/condition.py`
   hands the grammar the run's context and the plan's exits and knows nothing
   about ceilings, the same way it knows nothing about customer columns. That
   is what keeps a call-square concern out of a generic word, and it is what
   makes the typo `run.max_calls_reachd` a sentence the author reads at
   publish instead of a run that takes `else` for the life of the plan — the
   guard `context.<key>` cannot give, because a producer's facts are unbounded
   and a misspelt one is indistinguishable from a real one.

   Storing it would have been wrong four ways at once: stale across midnight
   (the ledger un-caps itself by re-stamp, a flag needs a write only a PLACED
   call makes — and a capped run places none); seedable by a producer, since a
   plain context key is admitted verbatim by `entry._context_from_payload`;
   leaked outward on the lead payload and the merchant's outcome webhook via
   `run_facts`; and unreadable by a template anyway, because `send_variables`
   refuses a bool and parks the run. Living in `run.` rather than `context.`
   ends the second outright: `context.max_calls_reached` and
   `run.max_calls_reached` are two different fields, so there is nothing for a
   merchant's payload to collide with.

4. **The walker records it on the trail (one small, general change).** Today
   `_advance` sets a square's `outcome` only for branching squares (from
   `reply_<node>`); a plain square's row carries NULL. A node's patch may now
   carry `_outcome`, which the walker POPS before `context.update(patch)` (so it
   is never written to the run's context and can never age into a later visit)
   and records as that square's outcome. `dispatched` stays None, `next_node` is
   the normal arrow. General on purpose — any plain square that one day has
   something to say about how it was left uses the same key. `pick_next`,
   `branches` and the registry are unchanged.

   *Not* a branch. Making `call` branch would break every board: `pick_next` on
   a branching node with no `reply_<node>` looks for `timeout`, then `else`,
   then returns None → "completed" after every ordinary call.

5. **Reporting back — every surface exists; nothing new is built.**
   - the trail (T26): the call square's row reads `outcome = max_calls`,
     `next_node` = the arrow taken, `dispatch_id` NULL — the console shows the
     square walked through without a call. `outcome` is free text
     ("vocabulary in code, `steps.py`", no CHECK), so **no migration**;
   - logs: `logger.bind(lead_skip="max_calls", calls_today=n, day=...)` — the
     alertable signal, beside `lead_id` on the placed path;
   - the run's context: the day ledger (`calls_today`) is visible on
     `GET runs/{id}`, and `run.max_calls_reached` is computed from it for
     any `condition` square that names it;
   - the run's ending is whatever it would have been — `by_exit_reason` does not
     single these runs out; the trail and the log field do. `calls_per_customer`
     is built from leads, so a capped run counts exactly the calls it placed;
   - the merchant's `reporting_webhook_url` fires per CALL outcome (buddy side,
     `managers/calls.py`); a capped visit places no call, so nothing fires.

6. **Two consequences the author must know.**
   - **A listening wait after a capped call waits for a letter that never
     comes.** `after-call` listening for `call.completed` hears nothing and
     leaves by its alarm. Correct and safe, but a wasted window — put a
     `condition` on `run.max_calls_reached` between the two and route past
     it. `docs/crm/plans/cart-recovery-retry.json` is that shape, shipped as a
     validated example.
   - **The answer is about TODAY, read the moment it is asked.** Because it is
     computed rather than stored, a `condition` anywhere on the board reads the
     live ledger against the live ceiling — at 09:00 the next morning the
     allowance has reset and the square says so, and a ceiling taken off the
     plan frees the run on the next question. Nothing goes stale, so nothing
     has to be judged "immediately after the call square".

7. **Publish laws (PURE).**
   - `max_calls_per_day` below 1, an unknown `timezone`, or a ceiling whose
     `timezone` is explicitly `null` — all refused by the model, so
     `validate_definition` reports them as shape problems;
   - a ceiling on a board with no `call` square is refused: a word that can
     never bind is a lie the author reads later. Read straight off the
     validated model — nothing defaults the ceiling and the stamp is only
     given to a board that has a call square, so a value here is one the
     author typed;
   - **a call square that can reach a call square again without the run
     waiting is refused** (`_call_loops_without_waiting`). The walker runs
     consecutive IMMEDIATE squares under one claim, so `call → call` and
     `call → condition → call` mint every lead of the loop inside a single
     visit — milliseconds apart, same phone, same `next_attempt_at` — until
     `_MAX_STEPS_PER_VISIT` parks the run as a runaway. The park is permanent,
     and nothing written during that visit reaches the lease CAS, so the very
     ledger this ceiling counts from is never persisted: without this law the
     bound cannot bound the one shape it exists for. A safety claim about
     merchant-authored documents is a publish law, never a per-document test.
     One wait anywhere on the path is enough — that is what ends the visit and
     lets the ledger commit, and it is what every shipped board already does.

8. **Docs, same commit (the triple).** The cart runbook's arrow-back bullet is
   rewritten (it claimed the opposite), with the two-layer note, the
   listening-wait remedy, the staleness caveat and the webhook note; its
   settings table gains `max_calls_per_day` and `timezone`;
   `18-outcome-feedback.md`'s bullet is marked superseded; `docs/crm/plans/`
   gains `cart-recovery-retry.json` and its README row.

## Red tests (each fails on `release` today)

- `tests/crm/test_workflow_call_visits.py`: at today's ceiling `execute` returns
  the trail word and **nothing else**, and **no** accessor is called, with the
  ledger untouched; **a new day resets the count** (yesterday's record is
  replaced, not added to); a placed call advances the ledger and writes nothing
  besides; **the visit counters are never reset by the day** (the id scar); the
  ceiling counts every call square of the run; the day is read on the plan's
  clock, not the server's (the same two instants judged under IST and UTC); junk
  and a stale day read as 0; `max_calls_reached` is the one predicate both
  squares ask, and neither it nor the trail word can reach a template; the
  `condition` square routes on the computed answer; taking the ceiling away
  frees the run on the next question; a board that names no ceiling grows no
  ledger.
- `tests/crm/test_workflow_steps.py`: `_outcome` is popped (absent from the
  persisted context), recorded as the step's `outcome`, `dispatch_id` NULL,
  `next_node` the plain arrow; the run walks on; the ledger is persisted and
  `max_calls_reached` is not.
- `tests/crm/test_workflow_plans.py`: a ceiling of 0 refused; a ceiling with no
  clock, and with an unknown clock, refused; a ceiling on a board with no call
  square refused; the ceiling is stamped into new rows and never onto old ones
  (a stored document without the word validates and executes unchanged);
  `call → call` and `call → condition → call` refused, one wait on the way back
  enough; and `DEFAULT_MAX_CALLS_PER_DAY < _MAX_STEPS_PER_VISIT` pinned as an
  executable inequality, so neither dial can drift past the other silently.

## Acceptance

- Suite green; boundary guard clean (no new table, no cross-module SQL, no
  migration — the ledger is run context and the trail word is free text).
- A board `call → wait(call.completed) --NO_ANSWER--> call` with
  `max_calls_per_day: 2` places two calls today, the third visit places none and
  walks on, and tomorrow it places two more.
- A document that does not name the ceiling — every plan stored before this
  phase — validates and executes exactly as on `release`: nothing defaults it
  at read. A plan WRITTEN from now on carries `6` / `Asia/Kolkata` in its own
  document, where the author can read it.
- Runbook and phase-18 text no longer claim a back-arrow is absorbed.

## Decisions

1. **Placement vs the corpus — OPEN, needs Swaroop's ruling before merge.**
   `modules/05-outreach` and `design/execution-ledger.md:119` say contact caps
   are permission's (ADR 0018), and the interim frequency cap must not be built.
   This phase reads `max_calls_per_day` as the plan's OWN runaway ceiling — a
   sibling of `max_age_days`, bounding what the board does to itself — not a
   customer-contact-frequency policy, which stays the gate's (`may_contact`,
   1/day · 4/wk) and reaches voice only at the takeover phase (ADR 0010). It is
   deliberately NOT a cross-run or cross-plan count and never reads buddy's
   table, so it is not the second gate phase 19 forbids. **That reading is a
   ruling, not a review call.** If refused, the runbook correction still ships
   and the cap waits for the gate's `transactional.* | 10` circuit breaker.
2. **Semantics of the number** — calls a run may place **in a calendar day**
   (Rahul, 22 Sep 2026), not over the run's life. A lifetime cap is still
   expressible as `max_age_days` × the daily number; the reverse is not.
3. **What is counted** — the walker's own placements, from the run's ledger.
   Counting dials INCLUDING buddy's re-dials would need a read of
   `lead_call_tracker` through a contract (law 2) on every call square, and
   would let a template's `max_retry` change what a pinned plan does; the
   multiplicative composition is documented instead.
4. **On reaching it — skip the call and walk on** (Rahul, 22 Sep 2026). The
   alternatives, both declined: ending the run with a new `exit_reason`
   (a 063-style CHECK amendment plus a canon T20 amendment, and it stops every
   later square), and holding the run on the square until the day flips (a
   second meaning for the window hold). The trail word, the log field and the
   fact are the report-back instead.
5. **Scope of the word** — plan-wide only (this phase), or plan-wide plus a
   per-square `WorkflowNode.max_calls_per_day` override (one more validator law,
   the `stages.overrides` shape). Recommended: plan-wide first.

## Out of scope

- A labelled `max_calls` arrow out of a call square (route at the ceiling
  without a `condition`) — needs `pick_next` to accept one labelled arrow beside
  a plain one on a non-branching square; backlog.
- A first-class count in the day report.
- Cross-run / cross-plan call frequency — the gate's, at the voice takeover.
