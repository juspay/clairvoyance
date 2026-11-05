# Phase 17 — `stages` ladder sugar + loan funnel migration to one board

**Kind**: feat + docs · **PR title**: `feat(crm): stages ladder — an ordered funnel expands into the wait_event board; loan-dropoff becomes one pinned plan` · **Depends on**: 14, 15, 16 · **Notes**: §14.1, §16.2, §15.1 Phase 3

## Design
1. **Sugar**: `WorkflowDefinition.stages: Optional[Stages]` with `Stages = {order: [topic…] (≥2), idle_minutes, on_idle: {type: call|send, …}, after_action_minutes, restart_on_repeat: bool, overrides: {topic: {idle_minutes?, on_idle?, after_action_minutes?}}}`. A document with `stages` MUST NOT also carry `nodes`/`edges`/`entry` (validator refuses); the expander produces them.
2. **Expander** (`outreach/ladder.py`, PURE: `expand_stages(definition) -> WorkflowDefinition`): for stage i with topic T_i: node `at-<slug>` = `wait_event(key="$topic", topics=[T_{i+1}…T_n], minutes=idle, stage=T_i)`; `act-<slug>` = the `on_idle` node; `after-<slug>` = `wait_event(same downstream, minutes=after_action_minutes, stage=T_i)`; edges: `at→at-j` labelled `T_j` for every j>i, `at→act` labelled `timeout`, `act→after`, `after→at-j` labelled `T_j`, no edge out of `after` on timeout (→ `completed`). The LAST stage's `at-` is a plain `wait` (nothing downstream to listen for) → `act` → end. `entry` = `[{topic: T_i, start: at-<slug>} …]` with the definition's shared words. Goals untouched (the author writes them).
3. **Where expansion runs**: at `create_workflow`/`update_draft`/`publish` — store BOTH: `definition.stages` (the author's intent) and the expanded `nodes/edges/entry` (what the walker reads). Decision: expand on validate and store the expanded document with `stages` retained alongside, so the walker never learns about ladders and the console can re-edit the ladder. Re-expansion on every draft save is idempotent (node ids derive from topics).
4. **Loan funnel migration**: replace `docs/crm/plans/loan-dropoff/*.json` with one `loan-dropoff.json` (§16.2, `on_publish: pin`, `key: application_id`); runbook: publish the board (live on publish), pause the five clocks in the same minute, archive them a day later. *Amended 2026-09-03 while landing:* a paused plan's open runs do not finish — the walker snoozes them, and there is no drain status — so the clock runs open at the cutover (customers quiet for under 30 minutes) are listed before pausing and exit `ejected` on archive; keeping the clocks live beside the board would call the same customer twice. The phase-07 CI test validates the new document and asserts the expansion's edge set equals the computed downstream set (the "one missing arrow = a wrong call" guard).

## Red tests
- Expander: for 3 stages, exact node ids, edge labels, and that every `at-`/`after-` node lists ALL downstream topics; overrides apply; last stage is a plain wait; `stages` + `nodes` together refused.
- Idempotence: expanding twice yields the same document.

## Acceptance
- Suite green; boundary clean; runbook updated; §13/§14 "verdict" rows updated to "loan funnel is a board as of phase 17".

## Out of scope
- Console. Outcome branches after the call (18).

## Landed (PR #1075, 2026-09-03)
- `outreach/ladder.py::expand_stages(document) -> document` — dict in, dict out (the model requires `nodes`/`entry`, so expansion precedes model validation); the stored form is the ladder beside its board. A document may carry `nodes`/`edges`/`entry` only when they equal its expansion; a top-level `debounce_minutes`/`restart_on_repeat` on a ladder is refused (each door debounces by its stage's idle time and restarts per the ladder). Squares `at-`/`act-`/`after-<slug>`, slug = the topic's last segment with non-alphanumerics as `-`; publish refuses a ladder saved without its board (the copy is verbatim).
- Loan board: `cooldown_hours: 1` (the clocks' value — a stage letter delivered late, after the journey ended, must not open a run and call; §16.2 amended), `exits.max_age_days: 30`, the offer stage idles 120 minutes.
- Two consumer fixes the board needed (`entry.py`, `nodes.py`): a letter the run's current square listens for is its answer, never also its repeat (`_answered_by` — otherwise a `restart_on_repeat` door pushed back the alarm the wake had just set and every stage clock ran twice); the reply records `latest_letter`, and `run_facts` lets that letter's facts win over the founding letter's (the moving letter is heard on the square the run leaves, and the action executes as its own square, so "the current square's facts" were never the latest stage's).
