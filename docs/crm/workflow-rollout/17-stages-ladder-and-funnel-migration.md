# Phase 17 — `stages` ladder sugar + loan funnel migration to one board

**Kind**: feat + docs · **PR title**: `feat(crm): stages ladder — an ordered funnel expands into the wait_event board; loan-dropoff becomes one pinned plan` · **Depends on**: 14, 15, 16 · **Notes**: §14.1, §16.2, §15.1 Phase 3

## Design
1. **Sugar**: `WorkflowDefinition.stages: Optional[Stages]` with `Stages = {order: [topic…] (≥2), idle_minutes, on_idle: {type: call|send, …}, after_action_minutes, restart_on_repeat: bool, overrides: {topic: {idle_minutes?, on_idle?, after_action_minutes?}}}`. A document with `stages` MUST NOT also carry `nodes`/`edges`/`entry` (validator refuses); the expander produces them.
2. **Expander** (`outreach/ladder.py`, PURE: `expand_stages(definition) -> WorkflowDefinition`): for stage i with topic T_i: node `at-<slug>` = `wait_event(key="$topic", topics=[T_{i+1}…T_n], minutes=idle, stage=T_i)`; `act-<slug>` = the `on_idle` node; `after-<slug>` = `wait_event(same downstream, minutes=after_action_minutes, stage=T_i)`; edges: `at→at-j` labelled `T_j` for every j>i, `at→act` labelled `timeout`, `act→after`, `after→at-j` labelled `T_j`, no edge out of `after` on timeout (→ `completed`). The LAST stage's `at-` is a plain `wait` (nothing downstream to listen for) → `act` → end. `entry` = `[{topic: T_i, start: at-<slug>} …]` with the definition's shared words. Goals untouched (the author writes them).
3. **Where expansion runs**: at `create_workflow`/`update_draft`/`publish` — store BOTH: `definition.stages` (the author's intent) and the expanded `nodes/edges/entry` (what the walker reads). Decision: expand on validate and store the expanded document with `stages` retained alongside, so the walker never learns about ladders and the console can re-edit the ladder. Re-expansion on every draft save is idempotent (node ids derive from topics).
4. **Loan funnel migration**: replace `docs/crm/plans/loan-dropoff/*.json` with one `loan-dropoff.json` (§16.2, `on_publish: pin`, `key: application_id`); runbook: publish the board, pause the five clocks, let their open runs finish (≤ 1 day), archive them. The phase-07 CI test validates the new document and asserts the expansion's edge set equals the computed downstream set (the "one missing arrow = a wrong call" guard).

## Red tests
- Expander: for 3 stages, exact node ids, edge labels, and that every `at-`/`after-` node lists ALL downstream topics; overrides apply; last stage is a plain wait; `stages` + `nodes` together refused.
- Idempotence: expanding twice yields the same document.

## Acceptance
- Suite green; boundary clean; runbook updated; §13/§14 "verdict" rows updated to "loan funnel is a board as of phase 17".

## Out of scope
- Console. Outcome branches after the call (18).
