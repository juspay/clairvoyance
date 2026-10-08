# Call Outcomes: Facts In, One Word Out

Status: **PR 1 implemented** (branch `feat/call-outcome-columns`): facts recorded beside the legacy writes, plus the shadow check. PR 2 and the lifecycle and eval work are planned.
Owners: outcome flow, this document and PRs 1–2 (Rahul P); call lifecycle (Anshu); eval engine (Ravi Prasad).
Code: `app/schemas/breeze_buddy/outcomes.py` (vocabulary and `legacy_outcome()`), `app/database/accessor/breeze_buddy/call_outcome.py` (the write gate and the shadow check), migration `083_add_call_outcome_columns.sql`.

---

## 1. The problem

`lead_call_tracker.outcome` is one free-text `varchar(50)`. About thirty places write it, and the last writer wins:

| Owner | What it writes into `outcome` |
|---|---|
| Dispatcher (never dialled) | `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG`, `CALL_LIMIT_REACHED`, `ABORT`, `ABORTED` |
| Inbound policy | `BLOCKED_REJECT`, `BLOCKED_REDIRECT`, `CAPACITY_REJECTED` |
| Carrier status callback | `NO_ANSWER` for every failure: no-answer, busy, failed, timeout, cancel |
| Pipeline fallbacks | `BUSY` (idle timeout, hangup, global end, IVR with nothing chosen), `UNKNOWN`, `EARLY_HANGUP`, `TRANSFERRED`, `ended_by_widget`, `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING` |
| Agent (LLM functions, observers, IVR options) | the template's word, in its own casing (`confirmed`, `CONFIRM`, `VOICEMAIL`, …) |

So one word means different things depending on who wrote it last. `BUSY` is the idle-timeout fallback and also the LLM's "customer is busy"; a real busy line is stored as `NO_ANSWER`; a transfer overwrites the agent's word.

## 2. The target

**Writers record facts; one pure function computes the word; it is written once, at completion.**

- Every part of the system records only what it knows. Our platform records whether the call was set up, the provider records what happened on the line, the session records how it ended, and the agent records what it decided.
- `legacy_outcome(facts)` turns those facts into the `outcome` word. It is **byte-for-byte the word today's writers produce** (section 5), so every existing reader keeps working: retries, analytics, CRM plans, webhooks, the API.
- The post-call eval is the only thing allowed to change it. At completion, **final `outcome` = `eval_outcome` if the eval decided, otherwise `legacy_outcome(facts)`**.

The target call lifecycle:

`call.initiated` → the call takes place (agent, observers) → `await eval()` → topics queued (if enabled) → `call.completed`

## 3. Columns (migration 083)

Each column is named by who writes it.

**`lead_call_tracker`**

| Column | Type | Holds | Written by |
|---|---|---|---|
| `platform_status` | varchar(20) | `INITIATED`, `NOT_INITIATED`; the same values for outbound, inbound and widget (`call_direction` says which) | dispatcher, inbound policy and answer handler, Daily session start; again at completion, the carrier callback, reconcile and the reaper when the row has none |
| `platform_reason` | varchar(30) | with `INITIATED`: `DIALED` (a phone call, either direction) or `WEB_SESSION` (widget / Daily). With `NOT_INITIATED`, today's exact words: `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG`, `CALL_LIMIT_REACHED`, `ABORT`, `ABORTED`, `BLOCKED_REJECT`, `BLOCKED_REDIRECT`, `CAPACITY_REJECTED` | same |
| `provider_status` | varchar(20) | `ANSWERED`, `NOT_ANSWERED` (every unanswered result), `UNKNOWN` (no provider signal survived). Empty on a web session, which has no phone line | carrier callback, completion, reconcile, reaper |
| `provider_reason` | varchar(20) | `COMPLETED` with `ANSWERED`; with `NOT_ANSWERED`, why, in one spelling for every provider: `NO_ANSWER`, `BUSY`, `FAILED`, `CANCELED`, `TIMEOUT` | same |
| `provider_hangup_cause` | varchar(100) | the provider's own cause as sent (Plivo `HangupCauseName` / `HangupCause`, Twilio `SipResponseCode` / `ErrorCode`), diagnostics only. Recorded for unanswered calls; an answered call's `completed` callback writes nothing to the finished row | carrier callback (unanswered) |
| `session_end_reason` | varchar(30) | how an answered session ended (section 4) | pipeline ending paths, IVR walker, completion (transfer), reaper |
| `agent_outcome` | varchar(50) | the agent's word, **exactly as the legacy column stores it**: no trimming, no case change | outcome hook (LLM functions, `update_outcome`, observers), IVR options |
| `agent_outcome_source` | varchar(20) | who decided it: `LLM`, `IVR`, `OBSERVER` | same |
| `eval_outcome` | varchar(50) | the eval's word | eval |
| `eval_status` | varchar(20) | `PENDING`, `DONE`, `FAILED`, `SKIPPED` | eval / lifecycle |
| `eval_result_id` | uuid (plain for now; the FK to `evaluation_result(id)` ships with its index when the eval writes the column) | the eval's result row | eval |

**`chat_session`**: `agent_outcome`, `agent_outcome_source`, `eval_outcome`, `eval_status`, `eval_result_id`. A chat has no platform set-up or phone line, and its `ended_reason` already records how a session ended.

All columns are nullable with no default, so `ADD COLUMN` changes only metadata. There are no CHECKs: the vocabulary lives in code, and a value the code doesn't know is dropped at the writer. There are no indexes yet; if a reader needs one, build it `CONCURRENTLY` by hand and ship a no-op migration (the 054 pattern).

## 4. Vocabulary

**`session_end_reason`**: the first ending recorded wins (`record_session_end_reason`). The exceptions are the transfer and the IVR system errors, which override like their legacy words do.

| Value | Set when |
|---|---|
| `AGENT_ENDED` | the flow's `end_conversation` (fallback from `metaData.call_ended_by = agent`); also an observer whose action is `end_conversation` (the voicemail observer) |
| `GLOBAL_END` | the LLM called the global `end_conversation` |
| `CUSTOMER_HANGUP` | the customer or client disconnected; an IVR hangup |
| `USER_IDLE_TIMEOUT` | user idle retries exhausted (overwrites the agent's word with `BUSY`) |
| `IDLE_TIMEOUT` | the pipeline's own idle disconnect (fills `BUSY` only when empty); the fallback for `metaData.call_ended_by = system`, since inside `end_conversation` only pipeline idle relies on it |
| `TRANSFERRED` | a successful transfer, set at completion |
| `EARLY_HANGUP` | transport setup failed before the agent started |
| `WIDGET_ENDED` | the widget visitor ended voice (see section 12: today this write never lands) |
| `PIPELINE_ERROR` | `end_call_with_errors` (setup error), passed by that path itself |
| `IVR_ENDED` | the caller reached an IVR END option |
| `IVR_NO_INPUT` | IVR retries exhausted with no key press |
| `IVR_ERROR` | IVR could not start: no socket or lead, voice refused, invalid flow |
| `IVR_LOOP_GUARD` / `IVR_NODE_MISSING` | the IVR walker's system errors (a missing node is rejected when the flow loads, so it becomes `IVR_ERROR` today) |
| `IVR_EXCEPTION` | the IVR walker raised mid-walk |
| `REAPED` | the stuck-call reaper closed the lead: a phone call that already had a word and no ending, or a web session (with or without a word) |

**Carrier status → provider facts:** `completed` → `ANSWERED` + `COMPLETED`; `no-answer` → `NOT_ANSWERED` + `NO_ANSWER`; `busy` → `NOT_ANSWERED` + `BUSY`; `failed` → `NOT_ANSWERED` + `FAILED`; `cancel` / `canceled` / `cancelled` → `NOT_ANSWERED` + `CANCELED`; `timeout` (Plivo network / carrier timeout) → `NOT_ANSWERED` + `TIMEOUT`. A status the map doesn't know is still `NOT_ANSWERED`, with no reason.

**Web sessions** are the `DAILY`, `DAILY_TEST` and `DAILY_STREAM` execution modes: `platform_reason = WEB_SESSION`, and the provider columns stay empty.

## 5. `legacy_outcome(facts)`

1. `platform_status` is `NOT_INITIATED` → `platform_reason`, copied as-is (only the refusal words; `DIALED` / `WEB_SESSION` are never a word).
2. `provider_status` is `NOT_ANSWERED` → `NO_ANSWER`; `UNKNOWN` → `UNKNOWN`.
3. `ANSWERED`, or empty on a web session. These endings **replace** the agent's word:

   | `session_end_reason` | Word |
   |---|---|
   | `TRANSFERRED` | `TRANSFERRED` |
   | `USER_IDLE_TIMEOUT` | `BUSY` |
   | `PIPELINE_ERROR` | `UNKNOWN` |
   | `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING` | that word |

4. With an agent word → the agent's word, raw.
5. No agent word:

   | `session_end_reason` | Word |
   |---|---|
   | `EARLY_HANGUP` | `EARLY_HANGUP` |
   | `WIDGET_ENDED` | `ended_by_widget` |
   | `CUSTOMER_HANGUP`, `IDLE_TIMEOUT`, `GLOBAL_END`, `IVR_ENDED`, `IVR_NO_INPUT`, `IVR_EXCEPTION` | `BUSY` |
   | `AGENT_ENDED` (the flow's end, no outcome) | empty, as today |
   | `REAPED`, or no ending at all with `ANSWERED` (carrier said completed, no pipeline) | `UNKNOWN` |

6. Chat, which has no platform or provider facts → the agent's word.
7. Final `outcome` = `eval_outcome` if set, otherwise the word from rules 1–6.

## 6. Every situation: what each column holds

`·` = empty (null). `outcome` here is the word without the eval, which is `legacy_outcome(facts)` and exactly today's word. How the eval changes it is in section 6.2. The golden tests in `tests/breeze_buddy/test_call_outcome_vocabulary.py` cover these rows. "DIALED" on rows 17–51 is `DIALED` for a phone call and `WEB_SESSION` for a widget / Daily session (which then has no provider facts).

### 6.1 Facts and `outcome`, per situation

| # | Situation | `outcome` | `platform_status` | `platform_reason` | `provider_status` | `provider_reason` | `provider_hangup_cause` | `session_end_reason` | `agent_outcome` | `agent_outcome_source` |
|---|---|---|---|---|---|---|---|---|---|---|
| **Not dialled** | | | | | | | | | | |
| 1 | Pre-check aborts | `PRECHECK_FAILED` | `NOT_INITIATED` | `PRECHECK_FAILED` | · | · | · | · | · | · |
| 2 | Blacklisted | `BLACKLISTED` | `NOT_INITIATED` | `BLACKLISTED` | · | · | · | · | · | · |
| 3 | No free number | `NUMBER_UNAVAILABLE` | `NOT_INITIATED` | `NUMBER_UNAVAILABLE` | · | · | · | · | · | · |
| 4 | Invalid phone | `INVALID_PHONE` | `NOT_INITIATED` | `INVALID_PHONE` | · | · | · | · | · | · |
| 5 | No config | `NO_CONFIG` | `NOT_INITIATED` | `NO_CONFIG` | · | · | · | · | · | · |
| 6 | Per-customer call limit | `CALL_LIMIT_REACHED` | `NOT_INITIATED` | `CALL_LIMIT_REACHED` | · | · | · | · | · | · |
| 7 | Abort: lead API / campaign / widget / demo | `ABORT` | `NOT_INITIATED` | `ABORT` | · | · | · | · | · | · |
| 8 | Abort: WooCommerce / CRM daily cap | `ABORTED` | `NOT_INITIATED` | `ABORTED` | · | · | · | · | · | · |
| **Inbound turned away** | | | | | | | | | | |
| 9 | Blocked, reject | `BLOCKED_REJECT` | `NOT_INITIATED` | `BLOCKED_REJECT` | · | · | · | · | · | · |
| 10 | Blocked, redirect | `BLOCKED_REDIRECT` | `NOT_INITIATED` | `BLOCKED_REDIRECT` | · | · | · | · | · | · |
| 11 | Over capacity | `CAPACITY_REJECTED` | `NOT_INITIATED` | `CAPACITY_REJECTED` | · | · | · | · | · | · |
| **Dialled, not answered** | | | | | | | | | | |
| 12 | No answer | `NO_ANSWER` | `INITIATED` | `DIALED` | `NOT_ANSWERED` | `NO_ANSWER` | carrier cause | · | · | · |
| 13 | Line busy | `NO_ANSWER` | `INITIATED` | `DIALED` | `NOT_ANSWERED` | `BUSY` | carrier cause | · | · | · |
| 14 | Call failed | `NO_ANSWER` | `INITIATED` | `DIALED` | `NOT_ANSWERED` | `FAILED` | carrier cause | · | · | · |
| 15 | Cancelled | `NO_ANSWER` | `INITIATED` | `DIALED` | `NOT_ANSWERED` | `CANCELED` | carrier cause | · | · | · |
| 16 | Plivo timeout (network / carrier) | `NO_ANSWER` | `INITIATED` | `DIALED` | `NOT_ANSWERED` | `TIMEOUT` | carrier cause | · | · | · |
| **Answered: the agent decided** | | | | | | | | | | |
| 17 | Agent decides, flow ends | agent word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `AGENT_ENDED` | agent word | `LLM` |
| 18 | Agent decides, customer hangs up | agent word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `CUSTOMER_HANGUP` | agent word | `LLM` |
| 19 | Agent decides, LLM global end | agent word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `GLOBAL_END` | agent word | `LLM` |
| 20 | Agent decides, then transferred | `TRANSFERRED` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `TRANSFERRED` | agent word | `LLM` |
| 21 | Agent decides, then user idle timeout | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `USER_IDLE_TIMEOUT` | agent word | `LLM` |
| 22 | Agent decides, then pipeline idle | agent word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IDLE_TIMEOUT` | agent word | `LLM` |
| 23 | Agent: "customer busy, call later" | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `AGENT_ENDED` | `BUSY` | `LLM` |
| 24 | Agent says "no answer" after talking | `NO_ANSWER` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `AGENT_ENDED` | `NO_ANSWER` | `LLM` |
| 25 | Voicemail observer (its `end_conversation` action ends the call) | `VOICEMAIL` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `AGENT_ENDED` | `VOICEMAIL` | `OBSERVER` |
| 26 | Observer set a word, then the LLM tries to change it | observer's word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | how it ended | observer's word (frozen) | `OBSERVER` |
| 27 | Agent decides, widget visitor ends | agent word | `INITIATED` | `WEB_SESSION` | · | · | · | `WIDGET_ENDED` | agent word | `LLM` |
| **Answered: nobody decided** | | | | | | | | | | |
| 28 | User idle timeout | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `USER_IDLE_TIMEOUT` | · | · |
| 29 | Pipeline idle | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IDLE_TIMEOUT` | · | · |
| 30 | Customer hangs up / disconnect | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `CUSTOMER_HANGUP` | · | · |
| 31 | LLM global end | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `GLOBAL_END` | · | · |
| 32 | Flow `end_conversation` action | empty | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `AGENT_ENDED` | · | · |
| 33 | Early hangup (setup failed before the agent) | `EARLY_HANGUP` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `EARLY_HANGUP` | · | · |
| 34 | Setup / pipeline error | `UNKNOWN` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `PIPELINE_ERROR` | · | · |
| 35 | Widget visitor ends (today `ended_by_widget`) | `ended_by_widget` | `INITIATED` | `WEB_SESSION` | · | · | · | `WIDGET_ENDED` | · | · |
| **IVR** | | | | | | | | | | |
| 36 | Option chosen | option word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | how it ended | option word | `IVR` |
| 37 | No input, timeout word set | timeout word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_NO_INPUT` | timeout word | `IVR` |
| 38 | END option, nothing chosen | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_ENDED` | · | · |
| 39 | No input, no timeout word | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_NO_INPUT` | · | · |
| 40 | Caller hangs up, nothing chosen | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `CUSTOMER_HANGUP` | · | · |
| 41 | Setup error / voice refused / invalid flow | `IVR_ERROR` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_ERROR` | · | · |
| 42 | Loop guard | `IVR_LOOP_GUARD` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_LOOP_GUARD` | earlier option word, if any | `IVR` if any |
| 43 | Node missing (can't happen today: the flow check rejects it at load, so it is row 41) | `IVR_NODE_MISSING` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_NODE_MISSING` | earlier option word, if any | `IVR` if any |
| 44 | Walker exception after an option | option word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_EXCEPTION` | option word | `IVR` |
| 45 | Walker exception, nothing chosen | `BUSY` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `IVR_EXCEPTION` | · | · |
| **Safety nets** | | | | | | | | | | |
| 46 | Carrier "completed", no pipeline finished the lead | `UNKNOWN` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | · | · | · |
| 47 | Reaper, pipeline ran, agent word | the word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | earlier ending, else `REAPED` | the word | as recorded |
| 48 | Reaper, pipeline died before any word (the reaper can't tell it from row 51) | `UNKNOWN` | `INITIATED` | `DIALED` | `UNKNOWN` | · | · | · | · | · |
| 49 | Reaper after a transfer (word `TRANSFERRED`) | `TRANSFERRED` | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | `TRANSFERRED` | agent's word | `LLM` |
| 50 | Reaper after a transfer, observer froze the word | the frozen word | `INITIATED` | `DIALED` | `ANSWERED` | `COMPLETED` | · | earlier ending, else `REAPED` | the frozen word | as recorded |
| 51 | Reaper, no word on the row (no pipeline ran, or the carrier result was lost) | `UNKNOWN` | `INITIATED` | `DIALED` | `UNKNOWN` | · | · | · | · | · |
| **Chat** | | | | | | | | | | |
| 52 | Chat session (`chat_session` table) | agent word | – | – | – | – | – | – | agent word | `LLM` |

A reaped web session has no provider facts: with a word it is row 47 without the provider columns; without one, its ending is `REAPED`, which gives the `UNKNOWN` the reaper writes. A refused inbound call (rows 9–11) still reached us, so `call_initiated_time` is set on it.

### 6.2 Eval columns and the final `outcome`

The eval engine fills these after the call.

| Situation | `eval_status` | `eval_outcome` | `eval_result_id` | Final `outcome` |
|---|---|---|---|---|
| Not dialled / turned away / not answered (rows 1–16, 48, 51) | `SKIPPED` | · | · | as in 6.1 |
| Answered, eval queued or running | `PENDING` | · | · | not final yet |
| Answered, eval agrees with the agent | `DONE` | the agent's word | result uuid | the word (same as 6.1) |
| Answered, nobody decided, eval decides | `DONE` | the eval's word | result uuid | **the eval's word** |
| Answered, eval disagrees with the agent | `DONE` | the eval's word | result uuid | **the eval's word** |
| Answered, eval can't decide | `DONE` | · | result uuid | as in 6.1 |
| Eval failed | `FAILED` | · | · | as in 6.1, or waits (open decision, section 10) |
| Chat | as the eval engine sets them | | | the agent's word until a chat eval exists |

Final `outcome` = `eval_outcome` whenever it is set, otherwise `legacy_outcome(facts)`. Until the eval engine and PR 2 land, the eval columns stay empty and the old writers still write `outcome`; the shadow check confirms it equals the `outcome` column in 6.1.

**Where the new flow differs from today, deliberately:** a few of today's words depend on timing races, which no pure function can reproduce. Each one now follows the precedence the code intends (decision 7):
- a fire-and-forget outcome write landing after `FINISHED` and overwriting the word;
- a hook's lead refresh undoing an idle-timeout `BUSY`;
- the widget end racing the bot's completion (rows 27 and 35);
- an ending that fires during an agent-to-agent transfer (`end_conversation` skips it and the call goes on with the new agent). The transfer clears the recorded ending, so the new agent's ending is the one read. Two sub-cases still differ:
  - the old agent had a word and the new agent sets none: the legacy word is the idle `BUSY`, the facts give the old word;
  - the new agent ends through the flow's own `end_conversation` action: stale `metaData` ending keys feed the fallback.

The shadow check counts how often each happens before PR 2.

## 7. End-to-end flow

| Stage | Pod | Records |
|---|---|---|
| Lead pushed | `main_server`, API | nothing |
| Dispatcher refusal / abort / inbound block / capacity | `main_server` | `platform_status = NOT_INITIATED` + `platform_reason` (terminal) |
| Dial (the provider placed the call) | `main_server`, dispatcher | `platform_status = INITIATED` + `DIALED` |
| Inbound call accepted | `main_server`, answer handler / voice pod | `INITIATED` + `DIALED` |
| Widget / Daily session starts | voice pod | `INITIATED` + `WEB_SESSION` |
| Carrier failure callback | `main_server`, API | `provider_status = NOT_ANSWERED`, `provider_reason`, `provider_hangup_cause` |
| Live call | voice pod (`agent_pool`, or `main_server` without pod isolation) | `agent_outcome` + `agent_outcome_source` on each decision, with the observer freeze applied; `metaData.outcome.*` fields as today; `session_end_reason` from the ending path |
| Call ends | voice pod | `provider_status = ANSWERED` + `COMPLETED` (none on a web session), and `session_end_reason = TRANSFERRED` after a transfer |
| Completed-call reconcile / stuck-call reaper | one `main_server` pod | `ANSWERED` + `COMPLETED` / `REAPED`, or `UNKNOWN` |
| Completion | today: voice pod or callback; after the lifecycle work: post-call worker | `outcome` (the eval's word, else `legacy_outcome(facts)`), `FINISHED`, retry, webhook, CRM `call.completed` |

## 8. PR 1: facts + function + shadow check (implemented, no behaviour change)

**What it does**
- **Migration 083:** the columns in section 3.
- **`outcomes.py`:** the vocabulary; `CallOutcome` (the facts one write carries); `initiated_call_outcome()` / `not_initiated_call_outcome()`; `provider_from_status()`; `agent_word()` (the raw word); `record_session_end_reason()` (first ending wins); `session_end_reason_from_meta()` (fallback from `metaData`); `completed_call_outcome()`; and the pure `legacy_outcome()`.
- **Every writer records its facts in the same statement as its legacy write,** which is left exactly as it was:

| Writer | Code | Facts |
|---|---|---|
| Dispatcher refusals | `dispatch/worker.py` (`_fail_and_release`, blacklist, refused telephony account) | `NOT_INITIATED` + `NO_CONFIG` / `NUMBER_UNAVAILABLE` / `INVALID_PHONE` / `BLACKLISTED` |
| Dial | `dispatch/worker.py` → `update_lead_call_details` | `INITIATED` + `DIALED` |
| Pre-check, call limit | `managers/calls.py` | `NOT_INITIATED` + `PRECHECK_FAILED` / `CALL_LIMIT_REACHED` |
| Aborts | `handle_lead_abort` (API, campaign, widget, demo), demo fallback, WooCommerce, CRM call-node daily cap (`crm/outreach/nodes/call.py`) | `NOT_INITIATED` + `ABORT` / `ABORT` / `ABORTED` / `ABORTED` |
| Inbound refusals | `services/inbound_policy.py`, `ivr/selection.py`, answer handler (capacity) | `NOT_INITIATED` + the word itself |
| Inbound accepted | answer handler, `agent/inbound.py` | `INITIATED` + `DIALED` |
| Hold-and-consult leg | `handlers/internal/hold_and_consult.py` | `INITIATED` + `DIALED` |
| Daily session start | `agent/__init__.py` → `update_lead_call_initiated_time_by_id` | `INITIATED` + `WEB_SESSION` |
| Carrier callback | `telephony/callbacks/handlers.py` → `handle_unanswered_calls` | `NOT_ANSWERED`, the mapped reason, `provider_hangup_cause` |
| Completion | `handle_call_completion`, `daily_completion_function` | `ANSWERED` + `COMPLETED` (none on a web session), the recorded ending, the agent's word; `TRANSFERRED` on a transfer; the set-up if the row has none |
| Ending paths | `agent/__init__.py` (user idle, pipeline idle / disconnect, early hangup), `agent/utils.py::end_call_with_errors`, `end_conversation_global`, widget end | `USER_IDLE_TIMEOUT`, `IDLE_TIMEOUT` / `CUSTOMER_HANGUP`, `EARLY_HANGUP`, `PIPELINE_ERROR`, `GLOBAL_END`, `WIDGET_ENDED` |
| IVR walker | `ivr/walker.py` | options → agent word (`IVR`); `IVR_ENDED` / `CUSTOMER_HANGUP` / `IVR_NO_INPUT`; `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING`, `IVR_EXCEPTION` |
| Outcome hook | `template/hooks.py` (LLM functions, `update_outcome`, observers) | agent word (`LLM` / `OBSERVER`); frozen once any observer has fired, matching the legacy guard |
| Reconcile / reaper | `managers/calls.py` | reconcile: `ANSWERED` + `COMPLETED`. Reaper: with a word, `ANSWERED` + `COMPLETED` (none on a web session) and `TRANSFERRED` when the word is `TRANSFERRED` after a transfer, else the earlier ending, else `REAPED`; with none, `UNKNOWN` (a web session: `REAPED`) |
| Agent-to-agent transfer | `agent/transfer.py::apply_transfer` | clears an ending the outgoing generation recorded |
| Chat | hook chat branch → `update_chat_session_outcome` | agent word (`LLM`) |
| Widget voice reuse | `reset_widget_voice_lead` | clears the facts with the legacy word |

- **Switch:** `CALL_OUTCOME_WRITES_ENABLED` (dynamic, default off, off if it can't be read), checked in one place: `accessor/breeze_buddy/call_outcome.py`. While it's off, every statement is byte-identical to today's, so the code can deploy before 083 runs.
- **Shadow check:** `check_legacy_outcome()` runs on every write that finishes a lead: the insert of a lead that is finished from the start, the completion, and the abort. It computes `legacy_outcome` from the row just written and compares it with the `outcome` there. The result is logged as `component=call_outcome_shadow`:
  - `shadow=match` (debug);
  - `shadow=mismatch` (warning, with both words and the facts);
  - `shadow=no_facts` (warning: a finished row where no writer recorded facts).

  It only reads, and a failure in it is logged and swallowed.
- **Tests:**
  - `test_call_outcome_vocabulary.py`: section 6 as a golden table, plus the mapping and storage rules.
  - `test_call_outcome_writes.py`: facts on each write path, and byte-identical SQL with the switch off.
  - `test_call_outcome_shadow.py`: every ending path's facts give back the word it writes; the shadow check and its wiring.
- **Removed from the earlier draft:** the daily Slack coverage report (the shadow check replaces it), the normalized agent word, and the raw provider status column (`provider_reason` keeps the provider's answer in one spelling, `provider_hangup_cause` its own cause).

**Rollout**
1. Deploy with the switch off.
2. Apply 083.
3. Set `CALL_OUTCOME_WRITES_ENABLED=true`.
4. Soak. Count `shadow=mismatch` and `shadow=no_facts` per `session_end_reason` / `platform_reason`, and explain every group.

**Done when:** mismatches are zero apart from the race cases (section 6), for at least 7 days of traffic. **Rollback:** switch off.

## 9. PR 2: switch over (planned)

- Remove every direct legacy write, fallback and override:
  - the `BUSY` / `TRANSFERRED` / `UNKNOWN` / `EARLY_HANGUP` / `ended_by_widget` / `IVR_*` writes;
  - the fill-if-empty defaults;
  - the observer override on the legacy column;
  - the mid-call legacy DB writes.

  Mid-call writes record facts only.
- Write `outcome` only at completion, from `legacy_outcome(facts)`, or from the eval's word once it exists.
- Move the readers that drive behaviour onto facts:
  - the retry decision (same `BUSY` / `NO_ANSWER` rule, on the computed word);
  - the abort's "empty outcome" check → no agent word and no pipeline;
  - reconcile and the reaper's "a pipeline ran";
  - early hangup's empty check;
  - `service_callback`'s mid-call `outcome` → `legacy_outcome(current facts)`.
- Readers of the stored word don't change, because it's the same word: analytics, CRM plans and letters, the Slack digest, the analysis skip, the leads API.
- The widget end's word follows the widget decision (section 12).
- Remove the shadow check and the switch.

## 10. Call lifecycle (Anshu) and eval engine (Ravi Prasad)

**Lifecycle**
- **The voice pod never waits for the eval.** At hangup it records the facts, releases the telephony channel and number (split `_release_call_resources` out of `handle_call_completion`), puts a job on a durable Redis post-call queue keyed by lead id, and returns.
- **A post-call worker on `main_server`** does the rest:
  1. awaits the eval for answered calls (other attempts skip it);
  2. writes the final `outcome`, `FINISHED`, the retry, the final merchant webhook and `call.completed`;
  3. queues topics.
- **Reliability:**
  - idempotent jobs, and a visibility timeout on each job;
  - the reaper learns the "waiting for eval" state;
  - abort treats that state as processing, and the carrier reconcile leaves it alone;
  - alerting on the backlog's age.
- The mid-call `service_callback` stays as it is.

**Eval engine.** PR #1207 has the engine, provider and client. The lifecycle needs one awaitable entry point: `evaluate_call(lead) → {status, outcome | None, result_id, error}`. It must:
- build its own context from the lead and the facts, with no "already `FINISHED`" gate and no skip based on the legacy word;
- run an outcome question whose options come from the template;
- store its `evaluation_result` row;
- write `eval_outcome`, `eval_status` and `eval_result_id` to the lead;
- **return** the result, or report failure, instead of logging and skipping.

#1207's migration also needs renumbering (080–083 are taken).

**Open:** whether the lifecycle falls back to `legacy_outcome(facts)` when the eval fails or times out, or waits for the eval to succeed.

## 11. Decisions

| # | Decision |
|---|---|
| 1 | An outcome is agent-driven: set by the live agent (LLM, IVR, observers) or by the eval. Platform, provider and pipeline results are facts, not outcomes |
| 2 | Writers record facts; `outcome` is computed by one pure function and written in one place |
| 3 | The computed word is byte-for-byte today's word. Only the eval may replace it (final = eval's word, else `legacy_outcome`) |
| 4 | Target lifecycle: `call.initiated` → call → `await eval()` → topics → `call.completed`; the voice pod hands off and never waits |
| 5 | `agent_outcome` is stored exactly as the legacy column stores the agent's word, raw casing included |
| 6 | `platform_reason` holds today's exact refusal words (`ABORT` vs `ABORTED`, `BLOCKED_REJECT` vs `BLOCKED_REDIRECT`, `CALL_LIMIT_REACHED`, `CAPACITY_REJECTED`), so no extra detail column is needed |
| 7 | Timing races follow the intended precedence. Widget end → the agent's word if any, else `ended_by_widget` |
| 8 | No flow-mode column: IVR endings have their own `session_end_reason` values (`IVR_ENDED`, `IVR_NO_INPUT`, `IVR_EXCEPTION`, …) |
| 9 | Two Clairvoyance PRs: PR 1 records facts and runs the shadow check with no behaviour change; PR 2 removes the old writers. Loom has one PR (the lead types and the leads / webhooks reference docs), rolled out after all the Clairvoyance work, lifecycle and eval included |
| 10 | Retries keep today's rule (`BUSY` / `NO_ANSWER`), read from the computed word; no per-template retry policy |
| 11 | The CI build pipeline keeps running only `tests/crm`; the Buddy suites are run locally before merging |
| 12 | No version suffixes in names: "call outcome", `CallOutcome`, `CALL_OUTCOME_WRITES_ENABLED` |
| 13 | Columns are named by who writes them: `platform_*` (set-up), `provider_*` (the line), `session_end_reason`, `agent_outcome*`, `eval_*`. Every reason is filled when its status says what happened: `DIALED` / `WEB_SESSION` for an initiated call, `COMPLETED` for an answered one |
| 14 | `provider_status` is `ANSWERED` / `NOT_ANSWERED` / `UNKNOWN`; why a call went unanswered is `provider_reason`, so `NOT_ANSWERED` never reads like the outcome word `NO_ANSWER` |
| 15 | `INITIATED` is recorded when the call is set up (the dial, the inbound accept, the Daily session start) and again by the writes that finish an initiated call when the row has none. A web session (widget / Daily) has no provider facts |

## 12. Open items

- The eval fallback question (section 10).
- Today no eval is queued for answered calls that never reach `end_conversation` (rows 33, 34, 46, 47, 49, 50). Decide whether the post-call worker evaluates them.
- **The widget end.** `/voice/end` writes `ended_by_widget` / `WIDGET_ENDED` by `lead.call_id`, which is the random Daily room name, so the write updates no row. The widget also closes its Daily connection first, so the bot records a customer disconnect: today a widget end is `CUSTOMER_HANGUP` → `BUSY` (or the agent's word), and widget voice runs `DAILY_STREAM` (no LLM on the voice lead; its words live on the chat session). Decide: keep that and drop the dead `WIDGET_ENDED` path, or fix the write in PR 2 (a behaviour change: widget ends would move from `BUSY` to their own word, still racing the bot's write).
- The Loom PR (types + reference docs) describes the final model: `outcome` keeps today's words, or the eval's word when it decided. It rolls out after all the Clairvoyance work. The final merchant webhook from the lifecycle work is documented there once it is designed.
- Existing issues found along the way, not caused by this work (today's word still matches, but they can surprise):
  - a dispatcher exception after `_acquire_number` never gives the telephony channel back;
  - `handle_call_completion` returns before its write when `_get_lead_config` finds nothing; the reaper closes the lead later;
  - an abort while the dispatcher is placing the call: the abort doesn't check the lock, so the lead ends `ABORT` while the phone rings;
  - the reaper takes any `PROCESSING` lead older than 10 minutes; the inbound grace period is switched off by a leftover `and False` (`managers/calls.py`), so a live call over 10 minutes is reaped (its channel released), then its completion overwrites the row;
  - an IVR option hook that calls `update_outcome_in_database` replaces the lead object the walker keeps writing to, so later option words, the `BUSY` default and the transcript are lost;
  - the reaper and reconcile can both close the same lead; each schedules its own retry;
  - `managers/calls.py` can't be imported on its own because of a circular import through `dispatch/__init__.py`. Harmless at runtime, fixable by making `dispatch/__init__.py` import `worker` lazily.
