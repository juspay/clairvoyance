# Phase 16 — Facts on resume, stage facts, parked runs movable, restart-on-repeat anywhere

**Kind**: feat · **PR title**: `feat(crm): events carry facts into the run; current node rides to templates; parked runs move; any node can re-arm on a repeat` · **Depends on**: 00, 15 · **Notes**: §14.1 prerequisite (3), §14.7 objections #2/#3, §16.2 (`current_stage`, `restart_on_repeat`), §16.3 G8

## Design
1. **Facts on resume, namespaced**: `resume_run_on_event_query` merges `{reply_<node>: answer, "facts": {"<node>": <scalar payload facts>}}` — nested under `facts.<node>` (`context || jsonb_build_object('facts', COALESCE(context->'facts','{}') || $x)`) so stage payloads never collide. `nodes.run_facts` flattens for templates: top-level facts first, then `facts.<current_node>`'s keys override (the most recent stage wins for the call), and exposes `facts_<node>_<key>` for explicit access. `facts` is bookkeeping (`_BOOKKEEPING_KEYS`).
2. **`current_node` / `current_stage`**: `run_facts` adds `current_node` (the run's node id) and, when the definition came from a `stages` ladder (phase 17) or the node carries a `stage` label (new optional `WorkflowNode.stage: Optional[str]`), `current_stage`. The call node passes it in the lead payload so one template can say "you stopped at {current_stage}". Test pins both keys.
3. **Parked runs movable by events**: `resume_run_on_event_query` and `cancel_open_runs_query` accept `status IN ('waiting','parked')` (cancel already does); a resumed parked run becomes `waiting` with `attempts=0` (the human's "resume" semantics, now event-driven). Docstring: "an event is evidence the customer moved; a parked run that hears it is no longer stuck on the thing that parked it".
4. **Restart-on-repeat anywhere** (#1041 generalised): `patch_open_run_query` drops the `current_node = <entry start>` restriction when the definition sets `restart_on_repeat: true` on the entry (new word) — then a repeat of the current node's OWN stage topic (the entry topic that started the run, or for ladder nodes the stage's topic) re-arms `wake_at = GREATEST(wake_at, now()+debounce)` and merges facts. Validator: `restart_on_repeat` requires `debounce_minutes > 0`.

## Red tests
- Queries: facts nested under `facts`; parked included in resume; patch query has/hasn't the node restriction per flag.
- `run_facts`: precedence (facts of current node override top-level), bookkeeping excluded, `current_node` present.
- Walker/entry (monkeypatched): a parked run receiving a listened event is resumed.

## Acceptance
- Suite green; boundary clean; §16.3 G8 → done.

## Out of scope
- Ladder expansion (17). List-shaped facts (G4, backlog).
