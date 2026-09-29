# Call Outcomes: Facts In, One Word Out

Status: **PR 1 implemented** (branch `feat/call-outcome-columns`): facts recorded beside the legacy writes, plus the shadow check. PR 2 and the lifecycle and eval work are planned.
Owners: outcome flow, this document and PRs 1–2 (Rahul P); call lifecycle (Anshu); eval engine (Ravi Prasad).
Code: `app/schemas/breeze_buddy/outcomes.py` (vocabulary and `legacy_outcome()`), `app/database/accessor/breeze_buddy/call_outcome.py` (the write gate and the shadow check), migration `081_add_call_outcome_columns.sql`.

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

- Every part of the system records only what it knows. The dispatcher records why the lead wasn't dialled, the carrier callback records what the carrier said, the pipeline records how the session ended, and the agent records what it decided.
- `legacy_outcome(facts)` turns those facts into the `outcome` word. It is **byte-for-byte the word today's writers produce** (section 5), so every existing reader keeps working: retries, analytics, CRM plans, webhooks, the API.
- The post-call eval is the only thing allowed to change it. At completion, **final `outcome` = `eval_outcome` if the eval decided, otherwise `legacy_outcome(facts)`**.

The target call lifecycle:

`call.initiated` → the call takes place (agent, observers) → `await eval()` → topics queued (if enabled) → `call.completed`

## 3. Columns (migration 081)

**`lead_call_tracker`**

| Column | Type | Holds | Written by |
|---|---|---|---|
| `connection_status` | varchar(30) | `NOT_DIALED`, `REJECTED`, `NO_ANSWER`, `BUSY`, `FAILED`, `CANCELED`, `ANSWERED`, `UNKNOWN` | dispatcher, inbound policy, carrier callback, completion, reconcile, reaper |
| `connection_reason` | varchar(50) | today's exact words: `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG`, `CALL_LIMIT_REACHED`, `ABORT`, `ABORTED`, `BLOCKED_REJECT`, `BLOCKED_REDIRECT`, `CAPACITY_REJECTED`, plus `TIMEOUT` (Plivo ring timeout) | same |
| `provider_status` | varchar(50) | the carrier's raw status (`no-answer`, `busy`, `failed`, `canceled`, `timeout`, `completed`) | carrier callback, reconcile |
| `hangup_cause` | varchar(100) | the carrier's raw cause, for diagnostics only | carrier callback |
| `end_reason` | varchar(50) | how an answered session ended (section 4) | pipeline ending paths, IVR walker, completion (transfer), reaper |
| `agent_outcome` | varchar(50) | the agent's word, **exactly as the legacy column stores it**: no trimming, no case change | outcome hook (LLM functions, `update_outcome`, observers), IVR options |
| `outcome_source` | varchar(20) | who decided it: `LLM`, `IVR`, `OBSERVER` | same |
| `eval_outcome` | varchar(50) | the eval's word | eval |
| `eval_status` | varchar(20) | `PENDING`, `DONE`, `FAILED`, `SKIPPED` | eval / lifecycle |
| `eval_result_id` | uuid (plain for now; the FK to `evaluation_result(id)` ships with its index when the eval writes the column) | the eval's result row | eval |

**`chat_session`**: `agent_outcome`, `outcome_source`, `eval_outcome`, `eval_status`, `eval_result_id`. A chat has no carrier leg, and its `ended_reason` already records how a session ended.

All columns are nullable with no default, so `ADD COLUMN` changes only metadata. There are no CHECKs: the vocabulary lives in code, and a value the code doesn't know is dropped at the writer. There are no indexes yet; if a reader needs one, build it `CONCURRENTLY` by hand and ship a no-op migration (the 054 pattern).

## 4. Vocabulary

**`end_reason`**: the first ending recorded wins (`record_end_reason`). The exceptions are the transfer and the IVR system errors, which override like their legacy words do.

| Value | Set when |
|---|---|
| `AGENT_ENDED` | the flow's `end_conversation` (fallback from `metaData.call_ended_by = agent`) |
| `GLOBAL_END` | the LLM called the global `end_conversation` |
| `CUSTOMER_HANGUP` | the customer or client disconnected; an IVR hangup |
| `USER_IDLE_TIMEOUT` | user idle retries exhausted (overwrites the agent's word with `BUSY`) |
| `IDLE_TIMEOUT` | the pipeline's own idle disconnect (fills `BUSY` only when empty) |
| `TRANSFERRED` | a successful transfer, set at completion |
| `EARLY_HANGUP` | transport setup failed before the agent started |
| `WIDGET_ENDED` | the widget visitor ended voice |
| `PIPELINE_ERROR` | `end_call_with_errors` (setup error) |
| `IVR_ENDED` | the caller reached an IVR END option |
| `IVR_NO_INPUT` | IVR retries exhausted with no key press |
| `IVR_ERROR` | IVR could not start: no socket or lead, voice refused, invalid flow |
| `IVR_LOOP_GUARD` / `IVR_NODE_MISSING` | the IVR walker's system errors |
| `IVR_EXCEPTION` | the IVR walker raised mid-walk |
| `REAPED` | the stuck-call reaper closed a lead a pipeline had run on (only fills a missing ending) |

**Carrier status → connection status:** `no-answer` → `NO_ANSWER`; `busy` → `BUSY`; `failed` → `FAILED`; `cancel` / `canceled` / `cancelled` → `CANCELED`; `timeout` → `NO_ANSWER` + reason `TIMEOUT`; `completed` → `ANSWERED`.

## 5. `legacy_outcome(facts)`

1. `NOT_DIALED` / `REJECTED` → `connection_reason`, copied as-is.
2. A carrier failure (`NO_ANSWER`, `BUSY`, `FAILED`, `CANCELED`) → `NO_ANSWER`.
3. `UNKNOWN` → `UNKNOWN`.
4. Answered. These endings **replace** the agent's word:

   | `end_reason` | Word |
   |---|---|
   | `TRANSFERRED` | `TRANSFERRED` |
   | `USER_IDLE_TIMEOUT` | `BUSY` |
   | `PIPELINE_ERROR` | `UNKNOWN` |
   | `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING` | that word |

5. Answered, with an agent word → the agent's word, raw.
6. Answered, no agent word:

   | `end_reason` | Word |
   |---|---|
   | `EARLY_HANGUP` | `EARLY_HANGUP` |
   | `WIDGET_ENDED` | `ended_by_widget` |
   | `CUSTOMER_HANGUP`, `IDLE_TIMEOUT`, `GLOBAL_END`, `IVR_ENDED`, `IVR_NO_INPUT`, `IVR_EXCEPTION` | `BUSY` |
   | `AGENT_ENDED` (the flow's end, no outcome) | empty, as today |
   | `REAPED`, or no ending at all (carrier said completed, no pipeline) | `UNKNOWN` |

7. Chat, which has no connection facts → the agent's word.

## 6. Every situation: what each column holds

`·` = empty (null). `outcome` here is the word without the eval, which is `legacy_outcome(facts)` and exactly today's word. How the eval changes it is in section 6.2. The golden tests in `tests/breeze_buddy/test_call_outcome_vocabulary.py` cover these rows.

### 6.1 Facts and `outcome`, per situation

| # | Situation | `outcome` | `connection_status` | `connection_reason` | `provider_status` | `hangup_cause` | `end_reason` | `agent_outcome` | `outcome_source` |
|---|---|---|---|---|---|---|---|---|---|
| **Not dialled** | | | | | | | | | |
| 1 | Pre-check aborts | `PRECHECK_FAILED` | `NOT_DIALED` | `PRECHECK_FAILED` | · | · | · | · | · |
| 2 | Blacklisted | `BLACKLISTED` | `NOT_DIALED` | `BLACKLISTED` | · | · | · | · | · |
| 3 | No free number | `NUMBER_UNAVAILABLE` | `NOT_DIALED` | `NUMBER_UNAVAILABLE` | · | · | · | · | · |
| 4 | Invalid phone | `INVALID_PHONE` | `NOT_DIALED` | `INVALID_PHONE` | · | · | · | · | · |
| 5 | No config | `NO_CONFIG` | `NOT_DIALED` | `NO_CONFIG` | · | · | · | · | · |
| 6 | Per-customer call limit | `CALL_LIMIT_REACHED` | `NOT_DIALED` | `CALL_LIMIT_REACHED` | · | · | · | · | · |
| 7 | Abort: lead API / campaign / widget / demo | `ABORT` | `NOT_DIALED` | `ABORT` | · | · | · | · | · |
| 8 | Abort: WooCommerce / CRM daily cap | `ABORTED` | `NOT_DIALED` | `ABORTED` | · | · | · | · | · |
| **Inbound turned away** | | | | | | | | | |
| 9 | Blocked, reject | `BLOCKED_REJECT` | `REJECTED` | `BLOCKED_REJECT` | · | · | · | · | · |
| 10 | Blocked, redirect | `BLOCKED_REDIRECT` | `REJECTED` | `BLOCKED_REDIRECT` | · | · | · | · | · |
| 11 | Over capacity | `CAPACITY_REJECTED` | `REJECTED` | `CAPACITY_REJECTED` | · | · | · | · | · |
| **Dialled, not answered** | | | | | | | | | |
| 12 | No answer | `NO_ANSWER` | `NO_ANSWER` | · | `no-answer` | carrier cause | · | · | · |
| 13 | Line busy | `NO_ANSWER` | `BUSY` | · | `busy` | e.g. `USER_BUSY` | · | · | · |
| 14 | Call failed | `NO_ANSWER` | `FAILED` | · | `failed` | carrier cause | · | · | · |
| 15 | Cancelled | `NO_ANSWER` | `CANCELED` | · | `canceled` | carrier cause | · | · | · |
| 16 | Plivo ring timeout | `NO_ANSWER` | `NO_ANSWER` | `TIMEOUT` | `timeout` | carrier cause | · | · | · |
| **Answered: the agent decided** | | | | | | | | | |
| 17 | Agent decides, flow ends | `confirmed` | `ANSWERED` | · | · | · | `AGENT_ENDED` | `confirmed` | `LLM` |
| 18 | Agent decides, customer hangs up | `confirmed` | `ANSWERED` | · | · | · | `CUSTOMER_HANGUP` | `confirmed` | `LLM` |
| 19 | Agent decides, LLM global end | `confirmed` | `ANSWERED` | · | · | · | `GLOBAL_END` | `confirmed` | `LLM` |
| 20 | Agent decides, then transferred | `TRANSFERRED` | `ANSWERED` | · | · | · | `TRANSFERRED` | `RESOLVED` | `LLM` |
| 21 | Agent decides, then user idle timeout | `BUSY` | `ANSWERED` | · | · | · | `USER_IDLE_TIMEOUT` | `confirmed` | `LLM` |
| 22 | Agent decides, then pipeline idle | `confirmed` | `ANSWERED` | · | · | · | `IDLE_TIMEOUT` | `confirmed` | `LLM` |
| 23 | Agent: "customer busy, call later" | `BUSY` | `ANSWERED` | · | · | · | `AGENT_ENDED` | `BUSY` | `LLM` |
| 24 | Agent says "no answer" after talking | `NO_ANSWER` | `ANSWERED` | · | · | · | `AGENT_ENDED` | `NO_ANSWER` | `LLM` |
| 25 | Voicemail observer | `VOICEMAIL` | `ANSWERED` | · | · | · | how it ended | `VOICEMAIL` | `OBSERVER` |
| 26 | Observer fired, LLM tries to change the word | first word | `ANSWERED` | · | · | · | how it ended | first word (frozen) | as first set |
| 27 | Agent decides, widget user ends | `confirmed` | `ANSWERED` | · | · | · | `WIDGET_ENDED` | `confirmed` | `LLM` |
| **Answered: nobody decided** | | | | | | | | | |
| 28 | User idle timeout | `BUSY` | `ANSWERED` | · | · | · | `USER_IDLE_TIMEOUT` | · | · |
| 29 | Pipeline idle | `BUSY` | `ANSWERED` | · | · | · | `IDLE_TIMEOUT` | · | · |
| 30 | Customer hangs up / disconnect | `BUSY` | `ANSWERED` | · | · | · | `CUSTOMER_HANGUP` | · | · |
| 31 | LLM global end | `BUSY` | `ANSWERED` | · | · | · | `GLOBAL_END` | · | · |
| 32 | Flow `end_conversation` action | empty | `ANSWERED` | · | · | · | `AGENT_ENDED` | · | · |
| 33 | Early hangup (setup failed before the agent) | `EARLY_HANGUP` | `ANSWERED` | · | · | · | `EARLY_HANGUP` | · | · |
| 34 | Setup / pipeline error | `UNKNOWN` | `ANSWERED` | · | · | · | `PIPELINE_ERROR` | · | · |
| 35 | Widget user ends | `ended_by_widget` | `ANSWERED` | · | · | · | `WIDGET_ENDED` | · | · |
| **IVR** | | | | | | | | | |
| 36 | Option chosen | option word | `ANSWERED` | · | · | · | how it ended | option word | `IVR` |
| 37 | No input, timeout word set | timeout word | `ANSWERED` | · | · | · | `IVR_NO_INPUT` | timeout word | `IVR` |
| 38 | END option, nothing chosen | `BUSY` | `ANSWERED` | · | · | · | `IVR_ENDED` | · | · |
| 39 | No input, no timeout word | `BUSY` | `ANSWERED` | · | · | · | `IVR_NO_INPUT` | · | · |
| 40 | Caller hangs up, nothing chosen | `BUSY` | `ANSWERED` | · | · | · | `CUSTOMER_HANGUP` | · | · |
| 41 | Setup error / voice refused / invalid flow | `IVR_ERROR` | `ANSWERED` | · | · | · | `IVR_ERROR` | · | · |
| 42 | Loop guard | `IVR_LOOP_GUARD` | `ANSWERED` | · | · | · | `IVR_LOOP_GUARD` | earlier option word, if any | `IVR` if any |
| 43 | Node missing | `IVR_NODE_MISSING` | `ANSWERED` | · | · | · | `IVR_NODE_MISSING` | earlier option word, if any | `IVR` if any |
| 44 | Walker exception after an option | option word | `ANSWERED` | · | · | · | `IVR_EXCEPTION` | option word | `IVR` |
| 45 | Walker exception, nothing chosen | `BUSY` | `ANSWERED` | · | · | · | `IVR_EXCEPTION` | · | · |
| **Safety nets** | | | | | | | | | |
| 46 | Carrier "completed", no pipeline finished the lead | `UNKNOWN` | `ANSWERED` | · | `completed` | · | · | · | · |
| 47 | Reaper, pipeline ran, agent word | the word | `ANSWERED` | · | · | · | earlier ending, else `REAPED` | the word | as recorded |
| 48 | Reaper, pipeline ran, no word | `UNKNOWN` | `ANSWERED` | · | · | · | `REAPED` | · | · |
| 49 | Reaper after a transfer (word `TRANSFERRED`) | `TRANSFERRED` | `ANSWERED` | · | · | · | `TRANSFERRED` | agent's word | `LLM` |
| 50 | Reaper after a transfer, observer froze the word | the frozen word | `ANSWERED` | · | · | · | earlier ending, else `REAPED` | the frozen word | as recorded |
| 51 | Reaper, no pipeline ran | `UNKNOWN` | `UNKNOWN` | · | · | · | · | · | · |
| **Chat** | | | | | | | | | |
| 52 | Chat session (`chat_session` table) | agent word | – | – | – | – | – | agent word | `LLM` |

### 6.2 Eval columns and the final `outcome`

The eval engine fills these after the call.

| Situation | `eval_status` | `eval_outcome` | `eval_result_id` | Final `outcome` |
|---|---|---|---|---|
| Not dialled / turned away / not answered (rows 1–16, 51) | `SKIPPED` | · | · | as in 6.1 |
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
| Dispatcher refusal / abort / inbound block / capacity | `main_server` | `connection_status` + `connection_reason` (terminal) |
| Dial | `main_server`, dispatcher | nothing new |
| Carrier failure callback | `main_server`, API | `connection_status`, `connection_reason` (`TIMEOUT`), `provider_status`, `hangup_cause` |
| Live call | voice pod (`agent_pool`, or `main_server` without pod isolation) | `agent_outcome` + `outcome_source` on each decision, with the observer freeze applied; `metaData.outcome.*` fields as today; `end_reason` from the ending path |
| Call ends | voice pod | `connection_status = ANSWERED`, and `end_reason = TRANSFERRED` after a transfer |
| Completed-call reconcile / stuck-call reaper | one `main_server` pod | `provider_status = completed` / `end_reason = REAPED` |
| Completion | today: voice pod or callback; after the lifecycle work: post-call worker | `outcome` (the eval's word, else `legacy_outcome(facts)`), `FINISHED`, retry, webhook, CRM `call.completed` |

## 8. PR 1: facts + function + shadow check (implemented, no behaviour change)

**What it does**
- **Migration 081:** the columns in section 3.
- **`outcomes.py`:** the vocabulary; `CallOutcome` (the facts one write carries); `agent_word()` (the raw word); `record_end_reason()` (first ending wins); `end_reason_from_meta()` (fallback from `metaData`); and the pure `legacy_outcome()`.
- **Every writer records its facts in the same statement as its legacy write,** which is left exactly as it was:

| Writer | Code | Facts |
|---|---|---|
| Dispatcher refusals | `dispatch/worker.py` (`_fail_and_release`, blacklist) | `NOT_DIALED` + `NO_CONFIG` / `NUMBER_UNAVAILABLE` / `INVALID_PHONE` / `BLACKLISTED` |
| Pre-check, call limit | `managers/calls.py` | `NOT_DIALED` + `PRECHECK_FAILED` / `CALL_LIMIT_REACHED` |
| Aborts | `handle_lead_abort` (API, campaign, widget, demo), demo fallback, WooCommerce, CRM call-node daily cap (`crm/outreach/nodes/call.py`) | `NOT_DIALED` + `ABORT` / `ABORT` / `ABORTED` / `ABORTED` |
| Inbound refusals | `services/inbound_policy.py`, `ivr/selection.py`, answer handler (capacity) | `REJECTED` + the word itself |
| Carrier callback | `telephony/callbacks/handlers.py` → `handle_unanswered_calls` | mapped status, `TIMEOUT`, `provider_status`, `hangup_cause` |
| Completion | `handle_call_completion`, `daily_completion_function` | `ANSWERED`, the recorded ending, the agent's word; `TRANSFERRED` on a transfer |
| Ending paths | `agent/__init__.py` (user idle, pipeline idle / disconnect, early hangup), `agent/utils.py::end_call_with_errors`, `end_conversation_global`, widget end | `USER_IDLE_TIMEOUT`, `IDLE_TIMEOUT` / `CUSTOMER_HANGUP`, `EARLY_HANGUP`, `PIPELINE_ERROR`, `GLOBAL_END`, `WIDGET_ENDED` |
| IVR walker | `ivr/walker.py` | options → agent word (`IVR`); `IVR_ENDED` / `CUSTOMER_HANGUP` / `IVR_NO_INPUT`; `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING`, `IVR_EXCEPTION` |
| Outcome hook | `template/hooks.py` (LLM functions, `update_outcome`, observers) | agent word (`LLM` / `OBSERVER`); frozen once any observer has fired, matching the legacy guard |
| Reconcile / reaper | `managers/calls.py` | `ANSWERED` + `completed` / `TRANSFERRED` when the word is `TRANSFERRED` after a transfer, else the earlier ending, else `REAPED`; `UNKNOWN` when no pipeline ran |
| Agent-to-agent transfer | `agent/transfer.py::apply_transfer` | clears an ending the outgoing generation recorded |
| Chat | hook chat branch → `update_chat_session_outcome` | agent word (`LLM`) |
| Widget voice reuse | `reset_widget_voice_lead` | clears the facts with the legacy word |

- **Switch:** `CALL_OUTCOME_WRITES_ENABLED` (dynamic, default off, off if it can't be read), checked in one place: `accessor/breeze_buddy/call_outcome.py`. While it's off, every statement is byte-identical to today's, so the code can deploy before 081 runs.
- **Shadow check:** `check_legacy_outcome()` runs on every write that finishes a lead: the insert of a lead that is finished from the start, the completion, and the abort. It computes `legacy_outcome` from the row just written and compares it with the `outcome` there. The result is logged as `component=call_outcome_shadow`:
  - `shadow=match` (debug);
  - `shadow=mismatch` (warning, with both words and the facts);
  - `shadow=no_facts` (warning: a finished row where no writer recorded facts).

  It only reads, and a failure in it is logged and swallowed.
- **Tests:**
  - `test_call_outcome_vocabulary.py`: section 6 as a golden table, plus the mapping and storage rules.
  - `test_call_outcome_writes.py`: facts on each write path, and byte-identical SQL with the switch off.
  - `test_call_outcome_shadow.py`: every ending path's facts give back the word it writes; the shadow check and its wiring.
- **Removed from the earlier draft:** the daily Slack coverage report (the shadow check replaces it), the normalized agent word, and the normalized reasons.

**Rollout**
1. Deploy with the switch off.
2. Apply 081.
3. Set `CALL_OUTCOME_WRITES_ENABLED=true`.
4. Soak. Count `shadow=mismatch` and `shadow=no_facts` per `end_reason` / `connection_reason`, and explain every group.

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

#1207's migration also needs renumbering (080 and 081 are taken).

**Open:** whether the lifecycle falls back to `legacy_outcome(facts)` when the eval fails or times out, or waits for the eval to succeed.

## 11. Decisions

| # | Decision |
|---|---|
| 1 | An outcome is agent-driven: set by the live agent (LLM, IVR, observers) or by the eval. Carrier, dispatcher and pipeline results are facts, not outcomes |
| 2 | Writers record facts; `outcome` is computed by one pure function and written in one place |
| 3 | The computed word is byte-for-byte today's word. Only the eval may replace it (final = eval's word, else `legacy_outcome`) |
| 4 | Target lifecycle: `call.initiated` → call → `await eval()` → topics → `call.completed`; the voice pod hands off and never waits |
| 5 | `agent_outcome` is stored exactly as the legacy column stores the agent's word, raw casing included |
| 6 | `connection_reason` holds today's exact refusal words (`ABORT` vs `ABORTED`, `BLOCKED_REJECT` vs `BLOCKED_REDIRECT`, `CALL_LIMIT_REACHED`, `CAPACITY_REJECTED`), so no extra detail column is needed |
| 7 | Timing races follow the intended precedence. Widget end → the agent's word if any, else `ended_by_widget` |
| 8 | No flow-mode column: IVR endings have their own `end_reason` values (`IVR_ENDED`, `IVR_NO_INPUT`, `IVR_EXCEPTION`, …) |
| 9 | Two Clairvoyance PRs: PR 1 records facts and runs the shadow check with no behaviour change; PR 2 removes the old writers. Loom has one PR (the lead types and the leads / webhooks reference docs), rolled out after all the Clairvoyance work, lifecycle and eval included |
| 10 | Retries keep today's rule (`BUSY` / `NO_ANSWER`), read from the computed word; no per-template retry policy |
| 11 | The CI build pipeline keeps running only `tests/crm`; the Buddy suites are run locally before merging |
| 12 | No version suffixes in names: "call outcome", `CallOutcome`, `CALL_OUTCOME_WRITES_ENABLED` |

## 12. Open items

- The eval fallback question (section 10).
- The Loom PR (types + reference docs) describes the final model: `outcome` keeps today's words, or the eval's word when it decided. It rolls out after all the Clairvoyance work. The final merchant webhook from the lifecycle work is documented there once it is designed.
- Existing issues found along the way, not caused by this work:
  - a dispatcher exception after `_acquire_number` never gives the telephony channel back;
  - `handle_call_completion` returns before its write when `_get_lead_config` finds nothing;
  - `managers/calls.py` can't be imported on its own because of a circular import through `dispatch/__init__.py`. Harmless at runtime, fixable by making `dispatch/__init__.py` import `worker` lazily.
