# A/06 — Outreach nits that touch the node/validator files (N12, N14, N15, N16)

**Track A · step 6** · **Kind**: fix · **PR title**: `fix(crm): outreach hygiene — dedupe message id, validator warnings, edge-label docs, one phone discovery (enh A/06)` · **Depends on**: A/05 merged (last in the track; touches the same files) · **Notes**: `../../workflow-rollout/context/nits.md`

| Nit | Fix | Test |
|---|---|---|
| N12 | `execute_send` on a dedupe hit (`queue_message` → None) looks up the existing row id via a new connectivity contract `message_id_for_dedupe(merchant, dedupe_key)` so `message_<node>` is always written (rollout 18's `match` on `message_id` depends on it being there). | dedupe path writes the id |
| N14 | Remove `entry._phone_from_payload` once every producer passes extractor handles; if a producer still lacks them, keep it and say why in the PR. | extractor path is the one used |
| N15 | Document edge labels in `schemas.WorkflowEdge`; validator WARNS on a label no `wait_event`/`condition`/`split`/`handoff` can produce. | warning emitted, publish not refused |
| N16 | Validator `warnings` list beside `problems` (surfaced in the create/draft/publish 200 body): orphan nodes, cycles, a listening node with no `timeout` edge. | each warning pinned |
