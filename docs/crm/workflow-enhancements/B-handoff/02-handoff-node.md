# B/02 — `handoff` node: park the run with a human, continue when they close it

**Track B · step 2** · **Kind**: feat · **PR title**: `feat(crm): handoff node — a run waits for a human to close its handoff, or times out (enh B/02)` · **Depends on**: B/01 merged · **Notes**: `../../workflow-rollout/context/reading-notes.md` §16.3 G6; rollout 18 (`match` on wait_event) is merged

## Design
- Node: `{id, type: "handoff", reason: str, assignee?: str, minutes: int}`. `NodeSpec(is_wait=True, execute=<open the handoff>)` — this is the first wait node WITH an arrival action, so relax the registry test's "is_wait ⇔ execute is None" to "is_wait ⇒ landing sets an alarm; execute (if any) runs on arrival". The walker runs `execute` when it ADVANCES onto the node (arrival), not when the alarm fires: add an `on_arrival` hook to the advance path in `walker._advance` (call `spec.execute` for the NEXT node before `advance_run` when `spec.arrives`), keeping today's behaviour for plain waits.
- Listening: the node behaves as a `wait_event` on topic `handoff.closed`, key `outcome`, with an implicit `match: {payload: "enrollment_id", run: "id"}` — implement by having the entry consumer's listening check ask the registry (`spec.listens`, `spec.topics_for(node)`, `spec.key_for(node)`) instead of `node.type == "wait_event"` (A/01 introduces `branches`/`listens`; if A/01 has not merged, add `listens` here and A/01 rebases — say so in the PR).
- Edges: labelled with the outcomes the plan expects plus `timeout` ("nobody picked it up"). Validator: labelled + distinct + `timeout` present.
- `run_facts` exposes `handoff_<node>` = the handoff id (bookkeeping prefix `handoff_`).

## Red tests
- Walker: advancing onto `handoff` opens exactly one handoff (idempotent on lease retry) and sets the alarm; a `handoff.closed` letter with matching `enrollment_id` and `outcome: "resolved"` resumes and takes the `resolved` edge; alarm → `timeout`; a letter for another run is ignored.
- Registry parity test updated for the arrival-action wait.

## Acceptance
- Suite green; boundary clean. Cart runbook gains the "VIP handoff" pattern (§16 adaptive flow).

## Decisions already made
- Arrival action + listening in one node rather than `http` + `wait_event`: a handoff is one thing to a merchant.
