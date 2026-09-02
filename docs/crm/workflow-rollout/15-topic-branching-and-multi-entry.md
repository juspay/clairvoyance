# Phase 15 — Topic branching, multi-topic entry, reply clearing

**Kind**: feat · **PR title**: `feat(crm): wait_event branches on $topic; entry as a list of {topic, start}; replies cleared on advance` · **Depends on**: 01, 13 · **Notes**: §13 Option B blockers, §14.1 prerequisites (1), (2), (4), §16.2

## Design
1. **`key: "$topic"`** on a `wait_event` node: in `entry.py`'s listening loop, `answer = event.topic if node.key == "$topic" else event.payload.get(node.key)`. Validator: `$topic` is the only `$`-word; any other `$…` is refused. `pick_next` unchanged (the edge `on` equals the topic string). Docs in `schemas.WorkflowNode`.
2. **Entry as a list**: `WorkflowDefinition.entry: Union[WorkflowEntry, List[WorkflowEntryAt]]` where `WorkflowEntryAt = WorkflowEntry + start: str`. Normalise in a `model_validator(mode="before")` to a list internally (`entries`), keeping `entry` accepted; single-entry plans get `start = nodes[0].id`. Validator: every `start` names a node; topics unique across entries; the shared words (`reenter`, `cooldown_hours`, `key`, `on_repeat`, `debounce_minutes`) may be given at the definition top level as defaults and overridden per entry.
   - `enrol.py`: `enrol(..., start_node)`; `_first_wake` uses the start node; `insert_enrollment` with `current_node=start`.
   - `entry.py`: `entry_matches` becomes `(flow, definition, entry_at)`; `_enrollment_key` reads `entry_at.key`.
   - `apply_repeat` (#1041) uses the matched entry's words and patches the run standing on **that entry's start node** (today hard-coded `nodes[0].id`).
   - Publish guard (`migrate` mode only, phase 11): "entry unchanged" compares the list.
3. **Clear reply keys on advance**: `walker._advance` — when moving OFF a `wait_event` node, delete `reply_<node.id>` from the context written by `advance_run`/`exit_run` so a later revisit of the same node (allowed once entries can start anywhere) cannot resolve on a stale answer. Pure helper `without_reply(context, node_id)` in `nodes.py` beside `run_facts`.

## Red tests
- Schema: `entry` object and list both validate; list with a bad `start` refused; `key: "$other"` refused; `$topic` accepted.
- Entry: `$topic` node → `resume_run_on_event` receives `{reply_x: "<topic>"}`; list entry → enrol called with the right `start` per topic.
- Walker: after advancing past `ask`, the written context has no `reply_ask`.

## Acceptance
- Suite green; boundary clean; `docs/crm/plans/` examples still validate (phase 07 test).

## Out of scope
- Facts on resume (16). Ladder sugar (17).
