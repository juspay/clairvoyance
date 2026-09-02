# Phase 34 — Hygiene sweep (nits N2–N3, N8–N17)

**Kind**: fix · **PR title**: `fix(crm): hygiene — total decoders, honest returns, docstring truths, seams` · **Depends on**: 06, 20 · **Notes**: `context/nits.md` · **Wave 7**. One PR, many small commits squashed to one; each item below is a bullet in the PR body with its test.

| Nit | Fix |
|---|---|
| N2 | Decide with Swaroop whether call nodes may use merchant-NULL (global) templates. If yes, state it in `execute_call`'s docstring; if no, park with "template is not this merchant's". (Ask before the PR.) |
| N3 | Verify after phase 06 that entry-side and walker-side goal predicates use the same `entered_event_at` COALESCE; add one test that pins both SQL strings to the same expression. |
| N8 | `platform/suppression.py::_load_dict/_load_list` → `shared/decode.jsonb_object/jsonb_list`. |
| N9 | `platform/suppression.py::entry_is_live` — keep, but call it from `is_suppressed` logging when a probe is True so it stops being test-only; or delete with its tests. Decision: keep + use. |
| N10 | `identity/facts.py::_assert_facts_in_txn` returns `bool` (False when the customer is missing); `record/workers.py` logs at warning on False. |
| N11 | `pg_trgm` GIN index on `crm_customer (merchant_id, display_name gin_trgm_ops)` — migration; enable the extension in the same file guarded by `CREATE EXTENSION IF NOT EXISTS`. |
| N12 | `execute_send` on a dedupe hit looks up the existing row id via a new connectivity contract `message_id_for_dedupe(merchant, dedupe_key)` so `message_<node>` is always written. |
| N13 | Reorder ingest dependencies so the size check (or phase 33's middleware) precedes auth. |
| N14 | Remove `entry._phone_from_payload` fallback once every producer passes extractor handles (voice mirrors pass them after the consumer-registry change); keep the test that the extractor path is used. If a producer still lacks it, leave and say why. |
| N15 | Document edge labels beyond `timeout` in `schemas.WorkflowEdge`; validator warns (not refuses) on a label that no `wait_event`/`condition`/`split` can produce. |
| N16 | Validator warnings (a `warnings` list beside `problems`, surfaced in the API 200 body): orphan nodes, cycles, a `wait_event` with no `timeout` edge. |
| N17 | `worker_main.start_worker_role` message states the per-role connection need (walker: three transiently). |

## Acceptance
- Each row has a test or a stated reason it cannot. Suite green; boundary clean.
