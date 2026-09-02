# Phase 18 — Outcome feedback into runs (G2, G3)

**Kind**: feat · **PR title**: `feat(crm): message and call outcomes reach the run — fallback branches and STOP handling` · **Depends on**: 15; **PRs #1040 (WhatsApp webhooks → spine) and #1052 (WhatsApp extractor) merged** — if not, build the call half only and note it. · **Notes**: §16.3 G2/G3, §3 (record consumers), `app/ai/voice/agents/breeze_buddy/crm_mirror.py` (call.attempted/completed already mirrored with `customer_id`)

## Design
1. **Call outcomes**: `call.completed` letters already carry `customer_id` and the lead's `outcome`; the lead carries `enrollment_id` (059). Make the outcome addressable by the run: the mirror payload (buddy side, `crm_mirror.py` MIRRORS row for `call.completed`) includes `enrollment_id` and `outcome`; a `wait_event(topics=["call.completed"], key="outcome")` after a `call` node branches on it (`on: "no_answer" → retry`, `on: "connected" → …`). Because the consumer resolves listening per customer (phase 13), add an optional `WorkflowNode.match: {"payload": "enrollment_id", "run": "id"}` so only THIS run's call resumes it (a customer can have two runs). Generic: `match` compares a payload field with a run field, reusing the goal-key predicate from phase 06.
2. **Message outcomes**: delivery receipts arrive via #1040 as spine letters (align the topic name to `connectivity/topics.py`; they carry `provider_message_id`). The dispatcher stamped `provider_message_id` on `crm_message`, and `crm_message.source_id` is the run id for workflow sends, so the mapping wamid → (run id, message id) exists. Add a connectivity contract `message_for_provider_id(merchant, provider_message_id) -> Optional[(source_id, message_id)]`. In the outreach consumer's listening loop, when a listened topic is a receipt topic, resolve the pair through that contract and inject `message_id` as a synthetic payload field before matching. Then a node such as `wait_event(topics=["message.status"], key="status", match={"payload": "message_id", "run": "message_<node>"})` resumes only the run that queued that message.
3. **STOP → suppression (G3)**: inbound WhatsApp letters (#1052 extractor) with body matching a STOP vocabulary (`app/crm/platform/stop_words.py`, in code) → a new record consumer in platform: `record_suppression(kind=phone, value, channel="whatsapp", reason="user_request", source="whatsapp_inbound", evidence_ref=event.id)`; registered in `worker_main.py` (rule 12). Fail closed on unparseable phone (skip with error log). Also cancel the customer's open runs with `exit_reason: withdrawn` (outreach consumer, listening on the same topic).

## Red tests
- Buddy mirror payload includes `enrollment_id`/`outcome` (tests/crm/test_crm_mirror.py).
- `match` predicate in `resume_run_on_event_query` and in the entry loop; a run without a matching field is not resumed.
- STOP consumer: "STOP", "stop ", "unsubscribe" → `record_suppression` called with E.164; garbage → not called.

## Acceptance
- Suite green; boundary clean (platform imports record contracts; outreach → connectivity contracts; buddy → crm contracts only).
- Cart runbook gains the fallback pattern (`call.completed` no_answer → send WA).

## Out of scope
- Email/SMS channels. Retry ladders as sugar.
