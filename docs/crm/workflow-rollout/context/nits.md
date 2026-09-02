> Nits from the app/crm + workflow read-through. Companion to crm-workflow-notes.md (bugs B1–B4, probable issues P1–P8 live there). A nit here is a consistency, hygiene, doc or ergonomics point — none changes behaviour a merchant or customer would notice on its own.

## Nits (17)

| # | Where | What | Suggested touch |
|---|---|---|---|
| N1 (W-7) | outreach/entry.py, outreach/walker.py `pick_next` | nodes.py says nothing matches a type string, but entry.py (`node.type == "wait_event"`) and pick_next (`node.type != "wait_event"`) do. | Add `listens`/`branches` flags to `NodeSpec` and ask the registry. |
| N2 (W-10) | outreach/nodes.py `execute_call` | Templates with `merchant_id IS NULL` (global) are accepted for any merchant's call node. Probably intended for shared templates. | Confirm and state it in the docstring. |
| N3 (W-11) | outreach/entry.py vs walker.py | Two definitions of "goal happened after entry": entry uses `entered_at < occurred_at` (NULL → cancel all), walker uses `COALESCE(occurred_at, received_at) > entered_at`. | Pick one predicate and reuse it. |
| N4 (C-2) | migration 056 comment | Says "NOTHING validates source_kind" — queue.py's `SOURCE_KINDS` now does. Merged migrations are immutable, so this is a doc drift only. | Note it in the corpus trail / building-modules.md. |
| N5 (C-3) | connectivity/onboarding.py | The credential is stored/rotated BEFORE the atom that may refuse (disabled door). A merchant re-running signup on a disabled door still rotates the vault row. | Move the disabled check ahead of `_store_credential` (it is already cheap via `identify()`), or accept and document. |
| N6 (C-4) | connectivity/onboarding.py `_STATUS_FOR_HEALTH` | `authenticated` → `degraded`, and only `healthy` may send, so a WABA whose subscribe call failed cannot send at all until re-onboarded. Deliberate, but ops-visible. | Surface a "re-run subscribe" action rather than full re-onboard later. |
| N7 (C-5) | connectivity/db/queries/template.py tail comment | Template webhook consumer (status/category/quality + crashed-submit resume) not built; templates stay `pending` and sends refuse `template_not_approved`. Documented gap. | Tracked in the corpus; nothing to do here. |
| N8 | platform/suppression.py `_load_dict`/`_load_list` | Not total (raise on a malformed jsonb) unlike shared/decode. Fine inside a single-row atom, but inconsistent with the house rule. | Use `jsonb_object`/`jsonb_list` from shared/decode. |
| N9 | platform/suppression.py `entry_is_live` | Only referenced from tests; the trigger is the authority. Executable documentation, but reads like dead code. | Comment says so already; optionally call it from `is_suppressed` logging or drop. |
| N10 | identity/facts.py `_assert_facts_in_txn` | Customer not found → logs error and returns silently. Callers (event worker) cannot distinguish "wrote facts" from "no such customer". | Return a bool or raise a domain error the worker logs. |
| N11 | identity/db/queries.py `list_customers_query` | `display_name ILIKE '%q%'` is a seq scan; docstring acknowledges pg_trgm as the follow-up. | Add the expression/trgm index when the list gets hot. |
| N12 | outreach/nodes.py `execute_send` | On a lease-retry dedupe (queue_message returns None) it returns `{}`, so `message_<node>` is never written to context for that run. Bookkeeping only. | Look up the existing row id by dedupe_key, or accept the gap. |
| N13 | record/api.py `within_size_limit` | Declared as a dependency AFTER `verified_caller`; since both depend on the parsed body, the order does not matter today, but the 413 fires after auth rather than before. | Put the size dep first if the intent is "cheapest refusal first". |
| N14 | record/extractors/flat.py vs entry.py `_phone_from_payload` | Two phone-discovery paths (extractor handles + entry fallback). The fallback exists for voice mirrors that resolve before the consumer; the docstring explains it, but it is the "two searches drift" pattern the file itself warns about. | Remove the fallback once voice mirrors pass handles too. |
| N15 | outreach/schemas.py `WorkflowEdge` | `Union[Tuple[str,str], Tuple[str,str,str]]` accepts any string as `on`; no vocabulary for labels beyond "timeout". | Fine while labels are merchant-defined; note it in the plan docs. |
| N16 | outreach/plans.py `validate_definition` | No reachability check (orphan nodes), cycles allowed, and a wait_event without a "timeout" edge exits `completed` on timeout rather than failing validation. | Add warnings (not refusals) or document the semantics. |
| N17 | app/crm/worker_main.py | Pool-ceiling guard is `>= 2`, but a walker visit can hold three connections transiently (claim conn released, then execute_call → lead accessor + update_lead_enrollment_id, plus queue_message). Not a hang risk since none nest, but the docstring's "two" is walker-inaccurate. | Reword the guard message per role, or leave as-is. |

## Added after reviewing PR #1041 (repeat entries)
| # | Where | What | Suggested touch |
|---|---|---|---|
| N18 | PR #1041 `repeat.py` `_as_number` | Accepts "nan"/"inf" strings; Postgres orders NaN above everything so a junk value always wins `refresh_max`. | `math.isfinite` guard (CodeRabbit finding, valid). |
| N19 | PR #1041 `patch_open_run_query` accumulate branch | `jsonb_array_length(context->'repeat_items')` errors if a producer payload carried a scalar `repeat_items` key (copied by `_context_from_payload`). | Filter bookkeeping keys in `_context_from_payload`, or `CASE jsonb_typeof(...) = 'array'`. |
| N20 | PR #1041 `entry._try_enrol` repeat call | Passes `_context_from_payload(event.payload)`, so a refreshed `phone` is not applied. Passing the full `context` would also overwrite `source_event_id`, which breaks the founding-event dedupe (P9). | Pass payload scalars + normalized `phone`, never `source_event_id`. |
| N21 | PR #1041 tests | SQL-shape and monkeypatch tests only; the founding-event redelivery case (P9) and the earlier-alarm case (P10) are not pinned. | Add red tests for both with the fixes. |
