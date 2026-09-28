# Call Outcomes: Connection, Agent and Eval Layers

Status: **Phase 1 implemented** (branch `feat/call-outcome-columns`). **Phase 2 implemented** (branch `feat/call-outcome-exposure`). Phases 3–4 planned.
Owners: Breeze Buddy team.
Code: `app/schemas/breeze_buddy/outcomes.py` (vocabulary), `app/database/accessor/breeze_buddy/call_outcome.py` (the write switch), migration `080_add_call_outcome_columns.sql`.

This file is the plan and the record of the decisions behind it: the problem, the target model, every phase with implementation directions, backward compatibility, and what the post-call eval (PR #1207) must provide.

---

## 1. The problem

`lead_call_tracker.outcome` is one free-text `varchar(50)` column, and the last writer wins. It started as a six-value CHECK in migration `001` (`NO_ANSWER, BUSY, CANCEL, CONFIRM, UNKNOWN, ADDRESS_UPDATED`). Migration `004` dropped the CHECK, and since then four different owners write into it:

| Real owner | Values it writes into `outcome` |
|---|---|
| Carrier (status callback) | `NO_ANSWER` for **every** failure: no-answer, busy, failed, timeout, cancel |
| Dispatcher (never dialled) | `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG`, `CALL_LIMIT_REACHED`, `ABORT` / `ABORTED` |
| Inbound policy | `BLOCKED_REJECT`, `BLOCKED_REDIRECT`, `CAPACITY_REJECTED` |
| Pipeline fallbacks | `BUSY` (idle timeout, hangup, `end_conversation_global`), `UNKNOWN`, `EARLY_HANGUP`, `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING`, `TRANSFERRED` (overwrites the agent's word) |
| Agent (LLM, IVR, observers) | `CONFIRM`, `CANCEL`, `ADDRESS_UPDATED`, `VOICEMAIL`, `BUSY` ("call me later"), anything a template's hook or observer writes |

This causes four problems:

- **One word means different things.** `BUSY` is the idle-timeout fallback *and* the LLM's "customer is busy". A real carrier busy is stored as `NO_ANSWER`. On 24 Sep 2026 `BUSY` was ruled "answered", because in Buddy it is the no-input timeout.
- **Retries are keyed on the mix.** `handle_call_completion` retries when `outcome in ["BUSY", "NO_ANSWER"]`. An invalid number (`failed`) is re-dialled like a missed call. A customer who asked for a call at 6 pm is re-dialled after `retry_offset`, not at 6 pm.
- **"Connected" has at least eight definitions:**
  - `_ANSWERED` in `queries/breeze_buddy/lead_call_tracker.py`
  - campaign `picked`
  - analytics `connected_leads`
  - attempts-to-connect
  - telephony-number `calls_picked`
  - the substring matching in `analytics/handlers.py`
  - the Slack digest
  - several places in Loom

  `VOICEMAIL`, `UNKNOWN` and `BLOCKED_*` count as connected in most of them.
- **System code overwrites the agent.** A successful transfer replaces the LLM's outcome with `TRANSFERRED`, and evals never write an outcome at all.

**Principle (decided):** an outcome is always agent-driven. It is set either by the live agent or by an eval over the interaction. Carrier, dispatcher and pipeline facts are recorded in their own layer and never in the outcome.

## 2. Target model

Three layers, each with one writer. They are written beside the legacy `outcome` until it is removed (Phase 4).

### 2.1 Columns

**`lead_call_tracker`** (migration 080)

| Layer | Column | Type | Written by |
|---|---|---|---|
| Connection | `connection_status` | varchar(30) | dispatcher, inbound policy, carrier callback, pipeline |
| | `connection_reason` | varchar(50) | same |
| | `provider_status` | varchar(50) | carrier status callback (raw status, e.g. `no-answer`, `busy`, `completed`) |
| | `hangup_cause` | varchar(100) | carrier status callback (raw cause, diagnostics only) |
| | `end_reason` | varchar(50) | pipeline ending paths; only when answered |
| Agent | `agent_outcome` | varchar(50) | LLM functions, `update_outcome`, IVR options, observers. Trimmed and upper-cased |
| | `outcome_source` | varchar(20) | same writer: `LLM`, `IVR`, `OBSERVER` |
| Eval | `eval_outcome` | varchar(50) | post-call eval (PR #1207) |
| | `eval_status` | varchar(20) | eval queueing and worker |
| | `eval_result_id` | uuid, FK `evaluation_result(id)` ON DELETE SET NULL | eval worker |
| Backfill | `backfilled_at` | timestamptz | one-off backfill job (Phase 2) |

**`chat_session`**: `agent_outcome`, `outcome_source`, `eval_outcome`, `eval_status`, `eval_result_id`. Chat has no carrier leg, and its existing `ended_reason` already records how a session ended.

**`call_execution_config`**: `retry_policy varchar(20) NOT NULL DEFAULT 'LEGACY'`, the per-template retry switch (Phase 3).

**Template outcome list**: `template.configurations.outcomes` (JSONB), shaped `[{name, description, is_success}]`. It is not a separate column, so template versioning (migration 077, `template_version`) snapshots it automatically.

### 2.2 Vocabulary

It lives in code (`app/schemas/breeze_buddy/outcomes.py`), never in CHECKs, per the repo rule. A value the code does not know is dropped at the writer, so it can never fail the legacy write it rides with.

| Enum | Values |
|---|---|
| `ConnectionStatus` | `NOT_DIALED`, `REJECTED`, `NO_ANSWER`, `BUSY` (the carrier's busy tone, never "call me later"), `FAILED`, `CANCELED`, `ANSWERED`, `UNKNOWN` |
| `ConnectionReason` | NOT_DIALED: `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG`, `CALL_LIMIT`, `ABORTED` · REJECTED: `BLOCKED`, `CAPACITY` · carrier: `TIMEOUT` |
| `EndReason` | `AGENT_ENDED`, `CUSTOMER_HANGUP`, `IDLE_TIMEOUT`, `TRANSFERRED`, `EARLY_HANGUP`, `IVR_NO_INPUT`, `IVR_ERROR`, `PIPELINE_ERROR` |
| `OutcomeSource` | `LLM`, `IVR`, `OBSERVER` |
| `EvalStatus` | `PENDING`, `DONE`, `FAILED`, `SKIPPED` |

**Reserved agent outcomes** are accepted by every template without being declared:
- `VOICEMAIL`: set by the voicemail observer or the eval. Once set it is never overwritten, and it is never retried.
- `CALLBACK_REQUESTED`: "call me later". Under the `CONNECTION` retry policy it is re-dialled exactly like today's `BUSY`.

**Carrier status mapping**:

| Raw status | Connection status | Reason |
|---|---|---|
| `no-answer` | `NO_ANSWER` | |
| `busy` | `BUSY` | |
| `failed` | `FAILED` | |
| `cancel` / `canceled` / `cancelled` | `CANCELED` | |
| `timeout` (Plivo) | `NO_ANSWER` | `TIMEOUT` |
| `completed` | `ANSWERED` | |

### 2.3 Where every legacy value goes

| Legacy `outcome` | Connection | End reason | Agent outcome |
|---|---|---|---|
| `NO_ANSWER` (carrier) | `NO_ANSWER` / `BUSY` / `FAILED` / `CANCELED` from the raw status | | |
| `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG` | `NOT_DIALED` + the same reason | | |
| `CALL_LIMIT_REACHED` | `NOT_DIALED` + `CALL_LIMIT` | | |
| `ABORT`, `ABORTED` | `NOT_DIALED` + `ABORTED` | | |
| `BLOCKED_REJECT`, `BLOCKED_REDIRECT` | `REJECTED` + `BLOCKED` | | |
| `CAPACITY_REJECTED` | `REJECTED` + `CAPACITY` | | |
| `BUSY` from idle timeout | `ANSWERED` | `IDLE_TIMEOUT` | empty (the agent's earlier word survives, if it set one) |
| `BUSY` from hangup / disconnect / global end | `ANSWERED` | `CUSTOMER_HANGUP` / `PIPELINE_ERROR` / `AGENT_ENDED` | empty |
| `BUSY` from the LLM ("call later") | `ANSWERED` | | `BUSY` today; `CALLBACK_REQUESTED` once templates declare lists |
| `EARLY_HANGUP` | `ANSWERED` | `EARLY_HANGUP` | |
| `UNKNOWN` (reaper, no outcome) | `UNKNOWN` | | |
| `UNKNOWN` (completed, no pipeline) | `ANSWERED` (`provider_status=completed`) | none (a dead pipeline and one that never started look the same) | |
| `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING` | `ANSWERED` | `IVR_ERROR` | never |
| `TRANSFERRED` | `ANSWERED` | `TRANSFERRED` | the agent's own word, kept |
| `VOICEMAIL` | `ANSWERED` | | `VOICEMAIL` (source `OBSERVER`) |
| `ended_by_widget` | `ANSWERED` | `CUSTOMER_HANGUP` | |
| Any agent word (`CONFIRM`, `confirmed`, …) | `ANSWERED` | from `call_ended_by` | the word, upper-cased |

### 2.4 Derived meanings

Nothing below is stored; each is computed from the columns.

- **Dialled**: `connection_status` is not `NOT_DIALED` and not `REJECTED`.
- **Answered**: `connection_status = ANSWERED`.
- **Reached a human**: answered, and the outcome is not `VOICEMAIL`.
- **Success**: the outcome is in the template's list with `is_success`.
- **Live and eval agree?**: compare `agent_outcome` with `eval_outcome`. The agreement is never a stored column.

### 2.5 Live outcome and eval outcome

- **No merged column is stored.**
- **Our own surfaces (Loom) show them side by side.** For the eval that means one of three states:
  - Filled: the live outcome was empty.
  - Confirmed: both are the same.
  - Flagged: they differ.
- **Existing customers keep reading the legacy `outcome`** until it is removed.
- **The eval sees the live outcome and adds to it, not a rival.** It fills an empty outcome, keeps a set one unless the transcript contradicts it, and flags a suggestion when it does.

## 3. Decisions (Q&A, 23–24 Sep 2026)

| # | Question | Decision |
|---|---|---|
| 1 | Who may set the agent outcome? | LLM, IVR and observers. Carrier, dispatcher and pipeline never |
| 2 | Voicemail | Reserved agent outcome `VOICEMAIL` (no separate `answered_by` column). Not retried |
| 3 | Live vs eval | No merged column. Shown separately in Loom; the legacy column serves existing customers |
| 4 | When the eval runs | Every answered call, **on by default** for every agent, per-template opt-out |
| 5 | Declared outcome lists | Required for new templates, optional for existing ones |
| 6 | Retries | Per template (`retry_policy`: `LEGACY` or `CONNECTION`); existing templates stay `LEGACY` until moved. `CONNECTION` rules are in Phase 3 (3b) |
| 7 | External systems | Additive keys for everyone. Consumers ignore unknown keys |
| 8 | Eval result to merchants | A **second webhook, opt-in only** |
| 9 | Legacy `outcome` | Frozen exactly as today until removal |
| 10 | Legacy removal | For everyone on one date, **to be agreed later**. No per-merchant flag |
| 11 | Chat | Included (agent and eval columns) |
| 12 | History | Best-effort backfill, rows marked with `backfilled_at` |
| 13 | Where the outcome list lives | `template.configurations.outcomes` |
| 14 | `agent_outcome` casing | Trimmed and upper-cased at write time. The legacy column keeps the original casing |
| 15 | Phase 1 monitoring | In-app daily scheduled task that posts to Slack |
| 16 | Lead API in Phase 1 | The new fields may appear (`model_dump`), null until writes are switched on |
| 17 | Naming | No version suffixes in names: "call outcome", `CallOutcome`, `CALL_OUTCOME_WRITES_ENABLED` |
| 18 | Post-call eval | Owned by PR #1207 (`POST_CALL_QUALITY`); see section 9 |
| 19 | Completed call with no pipeline | `ANSWERED` + `provider_status=completed`, **no** `end_reason`; `PIPELINE_NOT_STARTED` removed (not provable) |
| 20 | Second-webhook opt-in | `evaluation_config.configuration.notify_webhook`, admin-only |
| 21 | `FAILED` under `CONNECTION` | Never re-dialled until soak data justifies an allowlist of temporary causes |
| 22 | `CALLBACK_REQUESTED` | Re-dialled exactly like today's `BUSY`: `retry_offset`, counts toward `max_retry`, waits for calling hours. No `callback_at` scheduling |
| 23 | Answered, no agent outcome, under `CONNECTION` | Today's behaviour: re-dial on idle timeout, customer hangup, agent end and IVR no-input; not on early hangup, IVR error or setup error; reaper/reconcile always |
| 24 | Journey view | Keep canon's 12 columns; the journey read fetches the call outcome columns through a Buddy accessor |
| 25 | CI | The build pipeline keeps running only `tests/crm`; the Buddy suites (`tests/breeze_buddy`, including the call outcome tests) are run locally before merging |
| 26 | PR shape | Clairvoyance: one PR per phase. Loom: PR 1 carries Phases 1 and 2 (types and docs) and merges after Clairvoyance PRs 1 and 2, before `WEBHOOK_CALL_OUTCOME_KEYS` is turned on (its docs are the notice); PR 2 carries Phase 3 and merges together with Clairvoyance PR 3 (Loom first or in the same deploy) |

## 4. Backward-compatibility rules (every phase)

1. **Legacy fields are frozen.** Every path that writes `outcome` or `metaData.call_ended_by` / `call_end_reason` / `outcome.*` keeps doing so byte-for-byte. Every reader of them is untouched until Phase 4. That includes the `BUSY` fallback, the `TRANSFERRED` and observer overrides, retry-on-`BUSY`/`NO_ANSWER`, and the empty-`outcome` state checks.
2. **Same statement, and it can't break the legacy write.**
   - The new columns ride the same UPDATE or INSERT as the legacy write, through one optional `call_outcome: CallOutcome` argument, and only non-None values are written.
   - Values pass the vocabulary; an unknown value becomes empty.
   - There are no DB CHECKs on the new columns until Phase 4.
   - Building the value on a call path is wrapped, so an error is logged and never blocks the legacy write.
3. **Every behaviour change has a switch.** Either a dynamic flag (`CALL_OUTCOME_WRITES_ENABLED`, `WEBHOOK_CALL_OUTCOME_KEYS`) or a per-template value (`retry_policy`, evaluation config).
4. **Characterization first.** Pin a path's legacy value in a test before changing that path.
5. **Loom tolerates absence.** New fields are optional, and new sections hide when the data is missing, so Loom and Clairvoyance deploy in either order.
6. **Repo rules apply.**
   - One commit per PR.
   - Migrations take the next free number.
   - One table owner per migration: Buddy tables together, the CRM view on its own.
   - `CREATE INDEX CONCURRENTLY` is run by hand first (the 054 precedent).

## 5. Phases

| Phase | Ships | External change | Behaviour change | Switch |
|---|---|---|---|---|
| 1. Groundwork + write the new columns | Migration 080, vocabulary, every write path, daily coverage report | Lead API gains null fields | None | `CALL_OUTCOME_WRITES_ENABLED` |
| 2. Backfill + expose | Backfill, APIs, CRM event keys, webhook keys | Additive keys everywhere | None | `WEBHOOK_CALL_OUTCOME_KEYS` |
| 3. Adopt | Loom, outcome lists, connection-driven retries, CRM reports | Loom metrics on new definitions | Opt-in per template | `retry_policy` |
| 4. Sunset | Remove legacy `outcome` | `outcome` key removed | On the agreed date | Reversible until the column drop |

### Phase 1: groundwork and writing the new columns (implemented)

**What shipped**

- **Migration `080_add_call_outcome_columns.sql`:**
  - the columns in section 2.1, all nullable with no default;
  - `retry_policy DEFAULT 'LEGACY'`;
  - **no indexes.** The columns only exist once 080 runs, so their indexes can't be built `CONCURRENTLY` ahead of it, and a plain `CREATE INDEX` inside 080 would block every call write while it scans `lead_call_tracker`. Nothing reads the columns in Phase 1; the indexes ship in Phase 2.
  - 080 takes no long lock: every `ADD COLUMN` is metadata-only, and the new `eval_result_id` foreign key needs no validation scan because the column is new. Measured locally: 3.6 ms on a 3-million-row table.
- **Vocabulary and model:**
  - `app/schemas/breeze_buddy/outcomes.py`: enums, `CallOutcome`, the carrier mapping and the helpers (`call_outcome_from_lead`, `ended_session_call_outcome`, `completed_call_outcome`, `normalize_agent_outcome`, `hangup_cause_from_callback`).
  - `LeadCallTracker` gains the fields, and the decoder reads them with `row.get`, so a database without 080 still decodes.
- **Switch:** `CALL_OUTCOME_WRITES_ENABLED` (dynamic, **default off**), checked in one place (`accessor/breeze_buddy/call_outcome.py`), treated as off if it can't be read. While off, the query builders produce SQL byte-identical to before; this was verified against `release`.
- **Write paths:**

| Path | Code | Call outcome columns |
|---|---|---|
| Carrier status callback | `telephony/callbacks/handlers.py` → `handle_unanswered_calls` | mapped status + `provider_status` + `hangup_cause` |
| Completion (telephony) | `managers/calls.py::handle_call_completion` | `ANSWERED` (default), `end_reason`, agent outcome; a transfer sets `end_reason=TRANSFERRED` |
| Completion (Daily / widget) | `services/daily/daily.py::daily_completion_function`, widget `ended_by_widget` | `ANSWERED`; widget end sets `CUSTOMER_HANGUP` |
| Completed call with no pipeline | `reconcile_completed_call` | `ANSWERED` + `provider_status=completed`, no `end_reason` (fixed after review: an empty outcome can't prove the pipeline never started) |
| Stuck-call reaper | `reconcile_stuck_processing_leads` | `ANSWERED` + `PIPELINE_ERROR` if a pipeline ran, else `UNKNOWN` |
| Pre-checks | `_run_pre_checks_for_lead` | `NOT_DIALED` + `PRECHECK_FAILED` |
| Merchant call cap | `managers/calls.py` call-limit refusal (`CALL_LIMIT_REACHED`) | `NOT_DIALED` + `CALL_LIMIT` |
| Dispatcher | `dispatch/worker.py` (`_fail_and_release`, blacklist) | `NOT_DIALED` + `NO_CONFIG` / `NUMBER_UNAVAILABLE` / `INVALID_PHONE` / `BLACKLISTED` |
| Aborts | `handle_lead_abort`, WooCommerce cancel, demo cleanup | `NOT_DIALED` + `ABORTED` |
| Inbound blocks | `services/inbound_policy.py`, `ivr/selection.py` | `REJECTED` + `BLOCKED` / `CAPACITY` |
| Ending paths | `agent/__init__.py` (idle timeout, disconnect, early hangup), `agent/utils.py::end_call_with_errors`, `end_conversation` | `end_reason`; fallback from `metaData.call_ended_by` |
| Outcome hook | `template/hooks.py::UpdateOutcomeInDatabaseHook` | `agent_outcome` + `outcome_source` (observer detected via `metaData.observer_triggered`), taken **before** the legacy transfer / observer overrides; once any observer has fired (`observer_triggered`, even an alert with no outcome), a later LLM call does not replace an agent outcome already set, matching the legacy guard |
| IVR | `ivr/walker.py` | option / timeout outcome → agent outcome (`IVR`); walker errors → `end_reason=IVR_ERROR`; timeout → `IVR_NO_INPUT` |
| Chat | hook chat branch → `update_chat_session_outcome` | `agent_outcome` + `outcome_source` |
| Widget voice reuse | `reset_widget_voice_lead` | clears the per-call columns |

- **Coverage report:** `managers/call_outcome_coverage.py`, registered as `call_outcome_coverage`, runs daily. It posts to Slack:
  - coverage: the share of finished leads with a `connection_status`, plus uncovered rows grouped by legacy outcome, which names the write path;
  - consistency: whether the columns agree with the legacy word, with mismatches grouped.

  It tags on-call below 99.9% coverage or on any mismatch, and does nothing while the flag is off.
- **Tests:** `tests/breeze_buddy/test_call_outcome_{vocabulary,writes,coverage}.py`, plus call-outcome assertions in the dispatcher end-to-end tests.
- **Loom:** `src/lib/types/call-outcome.ts`; `LeadResponse` extends `CallOutcomeFields`.
- **Live verification (24 Sep 2026).**
  - **Setup:** the real server against a local Postgres 14 and Redis, driven through its HTTP and websocket endpoints, with every outbound request routed to a dead proxy.
  - **Deploy order:**
    - with the flag off, the new code ran against a database without 080;
    - 080 was applied by `scripts/migrate.py` while the server kept serving;
    - the flag was flipped on and off in Redis while the server ran.
  - **Paths exercised:**
    - dispatcher: blacklist, no number, call cap, pre-check abort;
    - `POST /lead/abort`;
    - Plivo status callbacks: `busy`, `timeout`, `completed`;
    - the stuck-call reaper;
    - inbound blocked and capacity-rejected calls;
    - a media-websocket setup error;
    - `GET /leads/{id}`;
    - the daily coverage report posting to Slack.
  - **Results:**
    - every path wrote the expected values;
    - the legacy `outcome`, retries and merchant webhook payloads were unchanged;
    - with the flag off, nothing was written to the new columns.
  - **Not exercised live** (no provider credentials; covered by unit tests): the invalid-phone refusal, and answered calls ended by an LLM or IVR.

**Rollout**

1. Deploy with the flag off.
2. Apply 080.
3. Set `CALL_OUTCOME_WRITES_ENABLED=true`.
4. Soak for 7 days on the daily report.

**Done when**
- coverage is at least 99.9% for 7 days;
- every mismatch group is explained;
- the legacy outcome distribution is unchanged week over week.

**Rollback:** set the flag to false.

**Effort:** backend 10–12 days, Loom 0.5 day; about 3 weeks of calendar time including the soak.

### Phase 2: backfill and additive exposure (implemented)

**Shape:** one Clairvoyance PR; the Loom side (docs and types) rides in Loom PR 1 with Phase 1 (decision 26). Nothing changes behaviour. Every new external key sits behind a switch that is off by default.

**What shipped**

| Part | Code |
|---|---|
| 2a CI | Not changed: the pipeline keeps running only `tests/crm` (decision 25) |
| 2b Backfill | `scripts/backfill_call_outcomes.py`. The legacy mapping tables (`LEGACY_NOT_DIALED_REASONS`, `LEGACY_REJECTED_REASONS`, `LEGACY_IVR_ERRORS`, `LEGACY_SYSTEM_FALLBACKS`) moved from the coverage report into `outcomes.py`, so the script and the report share them and the script never imports `app.ai` |
| 2c Indexes | `081_add_call_outcome_indexes.sql` (`IF NOT EXISTS`; the by-hand `CONCURRENTLY` procedure is in its header) |
| 2d Reads | `CallDetailResult` fields, CSV columns, the two filters, analytics types `connection-funnel`, `connection-breakdown`, `agent-outcome-breakdown`, `eval-agreement` (all dated on `created_at`, so `NOT_DIALED` attempts stay in), span attributes |
| 2e CRM | `call.completed` yielding facts; `call.outcome_evaluated` (`_evaluated_lead_tap`); `announce_call_evaluated` in the lead accessor; journey card via `get_call_outcome_columns` + `crm/record/timeline.py::with_call_outcomes` (fail-open: a failed read returns the cards without the fields) |
| 2f Webhooks | `utils/call_outcome_webhook.py` (keys + switch), the four builders, `callbacks/outcome_evaluated.py` (second webhook) |
| Tests | `tests/breeze_buddy/test_backfill_call_outcomes.py`, `tests/breeze_buddy/test_call_outcome_exposure.py` |

Where the code differs from the directions below:
- **`service_callback` runs inside the live conversation**, not after completion. Its keys say `connectionStatus=ANSWERED` (a pipeline is running) with `endReason=null` (the call has not ended yet), plus the agent outcome so far.
- **The second webhook is not behind `WEBHOOK_CALL_OUTCOME_KEYS`.** The template's admin-only `notify_webhook` is its switch, and only opted-in templates ever send it.
- **Webhook log labels** (`webhook=` on the final-delivery log): `service_callback`, `no_answer`, `precheck_failed`, `call_limit`, `call_outcome_evaluated`.
- **The call-detail list and grouped reads** select `lct.*` and needed no query change; only the CSV read names its columns.

**Prerequisites**
- The Phase 1 soak is done: at least 99.9% coverage for 7 days and every mismatch explained.
- The reconcile fix is in: `reconcile_completed_call` writes `ANSWERED` + `provider_status=completed` with **no** `end_reason`, and `EndReason.PIPELINE_NOT_STARTED` is removed. That state can't prove the pipeline never started.

**2a. CI (not changed)**
- The build pipeline keeps running only `pytest tests/crm -q` (decision 25). Run `uv run pytest tests -q` locally before merging; the Buddy suites (dispatcher end-to-end, call outcome writes, backfill, exposure) are not run in CI.

**2b. Backfill** (`scripts/backfill_call_outcomes.py`, standalone)
- **Connection:** copy `scripts/migrate.py`: an asyncpg pool from `POSTGRES_*` via dotenv. Don't import the app, which needs `JWT_SECRET_KEY`.
- **Mapping:** one pure function `classify(outcome, meta) -> column values`, unit-tested in `tests/breeze_buddy/test_backfill_call_outcomes.py`. It reuses the mapping tables in `managers/call_outcome_coverage.py`. Rules, in this order:

| Legacy `outcome` | Signal on the row | Columns |
|---|---|---|
| `PRECHECK_FAILED`, `BLACKLISTED`, `NUMBER_UNAVAILABLE`, `INVALID_PHONE`, `NO_CONFIG`, `CALL_LIMIT_REACHED`, `ABORT`, `ABORTED` | | `NOT_DIALED` + the matching reason |
| `BLOCKED_REJECT`, `BLOCKED_REDIRECT` / `CAPACITY_REJECTED` | | `REJECTED` + `BLOCKED` / `CAPACITY` |
| `NO_ANSWER` | `meta_data` is `{}` or null (the carrier path blanks it) | `NO_ANSWER`. Busy and failed can't be recovered |
| `NO_ANSWER` | meta has transcription / `outcome` / `call_ended_by` (an agent wrote the word) | `ANSWERED`, `end_reason` from `call_ended_by`, no agent outcome |
| `UNKNOWN` | `cleanup = completed_no_pipeline` | `ANSWERED` + `provider_status=completed` |
| `UNKNOWN` | `cleanup = stuck_processing_timeout`, or no signal | `UNKNOWN` |
| `UNKNOWN` | non-empty `errors` and `call_ended_by = system` (setup error) | `ANSWERED` + `PIPELINE_ERROR` |
| `EARLY_HANGUP` | | `ANSWERED` + `EARLY_HANGUP` |
| `IVR_ERROR`, `IVR_LOOP_GUARD`, `IVR_NODE_MISSING` | | `ANSWERED` + `IVR_ERROR` |
| `TRANSFERRED` | | `ANSWERED` + `TRANSFERRED`. The agent's word is lost to history |
| `ended_by_widget` | | `ANSWERED` + `CUSTOMER_HANGUP` |
| `BUSY` | `call_end_reason = user_idle_timeout` (**check first**; an idle timeout can follow a hook) | `ANSWERED` + `IDLE_TIMEOUT`, no agent outcome |
| `BUSY` | `metaData.outcome` present (the hook's trace) | `ANSWERED` + `agent_outcome=BUSY` (`LLM`) + `end_reason` from `call_ended_by` |
| `BUSY` | neither (fallback) | `ANSWERED` + `end_reason` from `call_ended_by` |
| any word | `observer_triggered` present | `ANSWERED` + the word upper-cased (`OBSERVER`) |
| any other word | | `ANSWERED` + the word upper-cased (`LLM`) + `end_reason` from `call_ended_by` |

- **Batching:** keyset on `id` with `status='FINISHED' AND connection_status IS NULL AND created_at < <cutover>`. Batch size 1000, sleep between batches, `--dry-run` prints the distribution, and a rerun resumes through the `IS NULL` predicate.
- **The write:** a dedicated `UPDATE … FROM unnest(...)` that sets the columns and `backfilled_at = now()`.
  - Never `update_lead_call_completion_details`: it's flag-gated, fires the CRM finished hook, and replaces `meta_data`.
  - Never touches `outcome`, `meta_data` or `updated_at`. The daily coverage report windows on `updated_at`, so leaving it alone keeps backfilled rows out of it.
- **Before the full run:** review about 200 sampled `BUSY` rows by hand.
- **Rollback:** `UPDATE … SET <columns> = NULL WHERE backfilled_at IS NOT NULL`.

**2c. Indexes**
- On production, by hand and outside a transaction: `CREATE INDEX CONCURRENTLY IF NOT EXISTS` for `idx_lct_connection_status`, `idx_lct_agent_outcome` and `idx_lct_eval_outcome`. Each is a partial index `WHERE <col> IS NOT NULL`.
- Then migration `081_add_call_outcome_indexes.sql` with the same statements without `CONCURRENTLY`. It's a no-op there (the 054 precedent).

**2d. Our read surfaces** (existing responses don't change shape except by added keys)
- **Lead API:** nothing to do; it already returns the fields (`model_dump`).
- **Call details:**
  - Add the optional fields to `CallDetailResult` (`schemas/breeze_buddy/analytics.py`) and map them in `_build_call_detail_result` (`api/routers/breeze_buddy/analytics/handlers.py`). The query already selects `lct.*`.
  - Update the pin in `tests/breeze_buddy/test_campaign_progress.py`.
- **CSV export:**
  - Add the columns to the explicit SELECT in `get_call_details_records_query`.
  - Append them **at the end** of `EXPORT_COLUMNS`. The default export includes every column, so appending keeps the existing column positions.
- **Filters:** `connection_status` and `agent_outcome` lists on `AnalyticsFilters`, applied in `build_analytics_where_clause`.
- **New analytics types.** Each follows the five-step pattern: an `AnalyticsType` value, a handler, an `_ANALYTICS_HANDLERS` entry, a query using `build_analytics_where_clause`, and an accessor. `attempts-to-connect` is the template.
  - `connection-funnel`: dialled → answered → reached a human → outcome decided. Success is added in Phase 3, once templates declare lists.
  - `connection-breakdown` and `agent-outcome-breakdown`.
  - `eval-agreement`: filled / confirmed / flagged, once #1207 fills `eval_*`.
  - Rows with no `connection_status` go into an explicit `unclassified` bucket. They are never silently counted as unreached.
  - Existing types and their "connected" definitions stay exactly as they are.
- **Langfuse:**
  - `update_span_with_evaluation_data` (`observability/tracing_setup.py`) sets `connection_status`, `agent_outcome` and `end_reason` beside `call_outcome`.
  - It takes them from the in-memory `call_outcome` that `end_conversation` already builds, so the attributes appear even while `CALL_OUTCOME_WRITES_ENABLED` is off.

**2e. CRM**
- **`call.completed` letters** gain `connection_status`, `connection_reason`, `end_reason`, `agent_outcome` and `outcome_source`.
  - Add them in both `_finished_lead_tap` and `_created_lead_tap` (`crm_mirror.py`); the second covers blocked inbound calls, which are finished at insert.
  - **Collision rule:** `mirror_to_crm` drops a merchant-declared field whose name equals one of ours. For these **new** keys the merchant's field keeps priority, and a warning is logged, so no existing letter changes. `outcome` is unchanged.
- **New topic `call.outcome_evaluated`:**
  - Add it to `MIRRORS` only. It must not go in the code `CATALOG`: `tests/crm/test_decode_engine.py` forbids a source in both, and call topics are deliberately uncatalogued.
  - A new accessor hook, `register_evaluated_hook` / `_evaluated_hooks` in `accessor/breeze_buddy/lead_call_tracker.py`, follows the created/finished hook pattern. #1207's write-back fires it.
  - The tap's payload carries `lead_id`, `call_id`, `enrollment_id` (so a waiting workflow can match it), `eval_outcome`, `eval_status` and `agent_outcome`.
  - The dedupe id is `eval_result_id`, so a re-evaluation isn't dropped.
- **Journey card:** the view keeps canon's 12 columns.
  - Add a Buddy accessor `get_call_outcome_columns(merchant_id, lead_ids)`, the same pattern as outreach's `get_call_facts_by_runs`.
  - The record module's journey read calls it for call-arm rows.
  - Add optional `connection_status`, `agent_outcome` and `eval_outcome` to `JourneyCard` (`crm/record/schemas.py`), and update `tests/crm/test_journey.py`.
- Existing workflow plans keep branching on `outcome` and don't change.

**2f. Merchant webhooks** (four builders, all through `send_webhook_with_retry`)

| Builder | Where the new values come from |
|---|---|
| `callbacks/service_callback.py` | `context.lead` after completion. Add keys **before** the merchant-field merge (`summary_data.update(extracted_fields)`), so a merchant's own field of the same name still wins |
| `_retry_call` `NO_ANSWER` webhook (`managers/calls.py`) | Pass the locally built `CallOutcome` in: `lead` there is the pre-write snapshot |
| Pre-check failure webhook (`managers/calls.py`) | The local `CallOutcome` |
| Call-limit webhook (`finish_lead_call_limit_reached`) | The local `CallOutcome` (or the `finished` row) |

- **Keys:** `event`, `connectionStatus`, `connectionReason`, `endReason`, `agentOutcome`, `outcomeSource`, `evalOutcome: {status, value}`.
  - They are always present, `null` when unknown, so the shape is stable.
  - Legacy keys, values and send conditions are unchanged.
- **Switch:** `WEBHOOK_CALL_OUTCOME_KEYS` (dynamic, default off, fails closed), following the `CALL_OUTCOME_WRITES_ENABLED` pattern.
- **Per-merchant monitoring:** `send_webhook_with_retry` logs the final status with `merchant_id` and webhook name, bound with `logger.bind`. This is an optional keyword, so the test stub `send_webhook_with_retry(session, url, data)` in `test_call_limits_dispatch.py` keeps working.
- **Opt-in second webhook `call.outcome_evaluated`:**
  - The Buddy side sends it from the evaluated-hook tap when the template's `evaluation_config.configuration.notify_webhook` is true. Only admins can set that.
  - Payload: the same legacy fields as the first webhook, plus `event` and `evalOutcome`.
  - It needs #1207.

**Loom (Phase 2, in Loom PR 1 with Phase 1)**
- Docs (`src/docs/reference/webhooks.svx`, `rest/leads.svx`): document the new keys and events, and mark `outcome` deprecated with the date to be announced.
- Types, all optional:
  - `CallDetail` and `AnalyticsFilters`, plus the new analytics result types;
  - `JourneyCard`;
  - `CallConfiguration.retry_policy`.

  Phase 3 UI can then start while the backend rolls out.

**Rollout**
1. Merge with every switch off.
2. Backfill: dry run, sample review, then the full run.
3. Indexes by hand, then migration 081.
4. Heads-up (changelog, docs, the Nautilus team) N days ahead.
5. Turn on `WEBHOOK_CALL_OUTCOME_KEYS` and watch each merchant's non-2xx rate.

**Done when**
- The backfill is complete and the sample review is signed off.
- The webhook keys are on with no increase in merchant errors.

**Rollback**
- Keys: turn `WEBHOOK_CALL_OUTCOME_KEYS` off.
- Backfill: reset the backfilled rows.
- The analytics types and CRM keys are additive.

**Effort:** backend 10–12 days, Loom 1–2 days; about 2–3 weeks of calendar time including the notice.

### Phase 3: move our consumers over, connection-driven retries

**Shape:** one Clairvoyance PR and Loom PR 2, merged together (Loom first or in the same deploy; decision 26). New behaviour is opt-in per template.

**Prerequisites:** Phase 2 is done (backfill complete, fields exposed), and `CALL_OUTCOME_WRITES_ENABLED` has been on and stable.

**3a. Template outcome lists** (Clairvoyance)
- **Model:** `ConfigurationModel.outcomes: Optional[List[OutcomeDefinition]]` (`template/types.py`, next to `observers`), where `OutcomeDefinition` is `{name, description, is_success}`.
  - Names are upper-cased and unique.
  - Descriptions are required.
  - Connection words (`NO_ANSWER`, `BUSY`, `UNKNOWN`, `FAILED`) are rejected; "call me later" is `CALLBACK_REQUESTED`.
  - The reserved outcomes are implied.
  - Add a `field_reference.json` entry, or the pre-commit test fails.
- **Checked on save, only when a list exists:**
  - A pure helper `validate_outcome_usage(flow, configurations)`, called from the create and replace handlers (`api/routers/breeze_buddy/templates/handlers.py`) and the assist-onboarding writer. A `ValueError` becomes a 400.
  - **Not** as a validator on `TemplateModel` / `ConfigurationModel`: those validate on read, so a stored template could stop loading at call time.
  - Version rollback is exempt; it restores a state that was valid when saved.
  - What the helper checks, normalized and compared against names ∪ `RESERVED_OUTCOMES`:
    - static `expected_fields.outcome` values of `update_outcome_in_database` hooks, in flow-mode node functions (a list), direct-mode functions, and IVR option hooks;
    - IVR `options[].outcome` and `on_timeout_outcome` (IVR `nodes` is a dict);
    - observer `action.args.outcome`.
- **The LLM is constrained to the list** (templates with a list only):
  - `template/builder.py::_build_function_schema` adds `enum` to `properties.outcome` when a hook reads the outcome from the LLM.
  - `template/global_function.py` (`BuiltinGlobalFunctionAdapter.build_schema`) does the same for the `update_outcome` builtin, reading the list from `bot_instance.template`.
  - Deep-copy `properties` first: templates are cached and the builder copies shallowly.
- **"Required for new templates":** Loom's create flow enforces it now. The public template API enforces it on the Phase 4 date.
- **Generator prompts** (`template/generator/prompts.py`):
  - produce `configurations.outcomes`;
  - replace the mandatory `customer_busy` → `BUSY` rule with `CALLBACK_REQUESTED`;
  - stop listing `NO_ANSWER` / `BUSY` as outcome names (the naming list, RULE 3/4, the checklist and the examples).

**3b. Connection-driven retries** (Clairvoyance)
- **The switch:**
  - `CallExecutionConfig.retry_policy: str = "LEGACY"`; the decoder reads it with `row.get("retry_policy") or "LEGACY"`.
  - It is switchable through the update query's explicit `_add(...)`.
  - The insert takes it in the column list only, **never** in `ON CONFLICT … DO UPDATE SET`: the conflict key is the template *name*.
  - `create_configuration_handler` sets `CONNECTION` for new configs. The DB default stays `LEGACY`, so no migration is needed.
  - Setting `CONNECTION` requires the template to declare an outcome list.
- **One pure decision function** `should_retry(policy, legacy_outcome, call_outcome)`, unit-tested. `LEGACY` returns exactly today's rule. `CONNECTION` works like this:

| The attempt ended as | Re-dial? |
|---|---|
| `NO_ANSWER`, line `BUSY`, `CANCELED` | Yes |
| `FAILED` | No, until the soak data justifies an allowlist of temporary causes |
| `NOT_DIALED`, `REJECTED` | No |
| `ANSWERED` + agent outcome `CALLBACK_REQUESTED` | Yes, like today's `BUSY`: after `retry_offset`, counts toward `max_retry`, waits for calling hours |
| `ANSWERED` + `VOICEMAIL` or any other agent outcome | No: the agent decided |
| `ANSWERED`, no agent outcome, `end_reason` `IDLE_TIMEOUT` / `CUSTOMER_HANGUP` / `AGENT_ENDED` / `IVR_NO_INPUT` | Yes (today's `BUSY` fallback) |
| `ANSWERED`, no agent outcome, `end_reason` `EARLY_HANGUP` / `IVR_ERROR` / `PIPELINE_ERROR` | No (as today) |
| Stuck-call reaper, completed-call reconcile | Yes, unconditionally (as today) |

- **Where it's called:**
  - `handle_call_completion` uses its in-memory `call_outcome`, which exists whatever the write flag is.
  - `handle_unanswered_calls` uses the status mapped from the carrier.
  - Tests stub the config as `SimpleNamespace`, so read the policy with `getattr(config, "retry_policy", "LEGACY")`.
- **Rollout:**
  1. Internal templates first.
  2. New configs start on `CONNECTION`.
  3. Existing configs switch one at a time with the merchant's agreement. The legacy `BUSY` word has to become `CALLBACK_REQUESTED` in the template first.

**3c. Internal reports** (Clairvoyance)
- **CRM reports:**
  - Add `_REACHED` next to `_ANSWERED` (`queries/breeze_buddy/lead_call_tracker.py`): `CASE WHEN connection_status IS NOT NULL THEN connection_status = 'ANSWERED' AND COALESCE(agent_outcome, '') <> 'VOICEMAIL' ELSE (<_ANSWERED>) END`. The fallback covers rows not yet backfilled.
  - `get_call_stats_by_runs` / `get_call_facts_by_runs` return `reached` beside `spoke` / `answered`.
  - `crm/outreach/analytics.py` switches to `reached`, and `tests/crm/test_console_reads.py` is updated.
  - `_ANSWERED` is removed after Loom's metric swap.
- **Campaign stats and workflow call reads:** add connection counts to `CampaignStats` (`queries/breeze_buddy/campaigns.py`) and `connection_status` to `RunCall` (`crm/outreach/runs.py`). Loom's campaign bar and run drawer need them.
- **Slack digest** (`services/langfuse/tasks/score_monitor/score.py`): dialled / answered / reached / success from the new columns, instead of the `CONFIRM` / `CANCEL` / `ADDRESS_UPDATED` / `BUSY` / `NO_ANSWER` literals.
- **Workflow plans:**
  - Add **new** files (e.g. `docs/crm/plans/cart-recovery-fallback-connection.json`) whose `after-call` wait uses `key: "connection_status"`, with edges `NO_ANSWER` / `BUSY` / `CANCELED` / `FAILED` / `NOT_DIALED` → fallback and `else` (answered) → next.
  - Register them in `tests/crm/test_plan_templates.py` and the plans README. Existing files stay as they are.
  - Ops re-publishes live workflows from them. **`cart-recovery-fallback` publishes with `migrate`, so open runs move to the new version.** Only publish once the write flag has been on long enough that every `call.completed` letter carries `connection_status`. A letter without the key is ignored until the wait's alarm (rule B1).

**3d. CI rule: no new reads of the legacy `outcome`** (Clairvoyance)
- A separate script, `scripts/check_outcome_reads.py`, because the CRM boundary script changes only through the corpus.
- It keeps a per-file ratchet allowlist `{path: max_count}`. The baseline comes from today's counts: `managers/calls.py` 5, `template/hooks.py` 3, `analytics/analytics.py` about 39 SQL references, and so on.
- It matches lead-like `.outcome` reads in Python and `"outcome"` in `app/database/queries` SQL. It ignores the other meanings of "outcome" (connectivity send outcomes, pre-checks, `IvrOption.outcome`, `metaData["outcome"]`).
- It ships as a triple: docs text, the script plus a CI step, and a failing test in `tests/crm/`.

**3e. Loom PR 2 (Phase 3)**
- **Shared module** `src/lib/console/call-outcome.ts` (tested in `__tests__/call-outcome.test.ts`):
  - `connectionBadge`, `endReasonLabel`, `agentOutcomeBadge` (success from `is_success`, reserved outcomes styled apart);
  - `evalState` (filled / confirmed / flagged);
  - funnel predicates.
  - It uses the existing badge classes, since `pnpm check:css` rejects raw colours. The legacy label shows only when every new field is null.
- **Conversation lists:**
  - `console/conversations.ts` (`ConversationRow` gains connection, outcome and eval badges; the `outcome || status` fallbacks go);
  - `ConversationsTable.svelte` (new columns behind `visCols`; extend `skeletonWidths`);
  - both list pages: new filters with **new saved-filter keys**, so stale legacy values are never sent. The agent list takes its outcome options from `configurations.outcomes` plus the reserved ones.
- **Conversation detail** (`conversations/[id]/+page.svelte`):
  - Connection and Outcome cards between Details and Call information.
  - An Eval card before Evaluations, guarded on `eval_status`.
  - Footer from `end_reason`.
  - Add `outcome`, `call_end_reason` and `observer_triggered` to `CALL_INFO_SKIP`.
- **Mixed lists removed:**
  - `NON_OUTCOME_KEYS`, the `outcomeBadge` pattern ladder, `OUTCOME_COLOR_PINS`, and `voice-metrics.ts` together with its hand-copied versions on the two channels pages;
  - the legacy `DashboardMetrics` / `AreaChart` and the `CallRecordsTable` badge ladder;
  - the campaign stack bar (fed by the new `CampaignStats` counts);
  - the `wf/perf/metrics.ts` labels, and the `EARLY_HANGUP` metric, which becomes an end reason. Update `wf-perf.test.ts`.
  - Also the other `outcome || status` fallbacks (agent overview, campaigns, `RunDrawer`), and the dead `getOutcomeConfig`.
- **Metrics:** new definitions shown **alongside** the old ones, labelled "(new)", with an in-app note, for one release cycle, then swapped. Slots:
  - a funnel card on workspace analytics (between the KPI rows and row 2) and agent analytics;
  - an eval-agreement card;
  - "Success rate · 7d" on the agent overview;
  - "Outcome completion" restored on Home.
- **Builder:**
  - An outcome-list editor in agent Settings (the draft + SaveBar model).
  - A Select for the static outcome value in `FlowHookItem.svelte`, fed through `flowsEditorStore`.
  - A Select for the observer outcome (`observers/+page.svelte`), with free text kept when there's no list.
  - IVR: validate the JSON only; Loom has no IVR editor.
  - The create wizard requires a list, prefilled by the generator.
- **Settings:** an outcome-eval toggle and the `notify_webhook` opt-in (both admin-only; #1207 endpoint), following the topic-eval toggle.
- **Channels:** a "Retry on" select (`LEGACY` / `CONNECTION`) under Retries. It's admin-only, with a confirmation when switching an existing config.
- **Workflows:**
  - `WaitInspector` gains "Branch on" (`connection_status`, `agent_outcome`, `end_reason`, `eval_outcome`, legacy `outcome`) and a value → target editor.
  - The starter `cartRecoveryFallback` moves to `connection_status`. Its early-hangup branch becomes a second wait on `end_reason`, or is dropped.
  - `plan.ts` gets key-aware labels, and `call.outcome_evaluated` gets a catalog label.
- **Journey card:** the connection badge when not answered, otherwise the agent-outcome badge, plus an eval chip when it differs. It falls back to the legacy `outcome`.

**Done when**
- The CI allowlist holds only the legacy emitters, the `LEGACY` retry path and the empty-outcome checks.
- Loom has swapped to the new metrics.
- New configs start on `CONNECTION`.

**Rollback**
- `retry_policy` back to `LEGACY` per config.
- The Loom metric swap is a release revert.
- Lists and validation only apply to templates that declare a list.

**Effort:** backend 12–15 days, Loom 15–18 days; about 4 weeks with both in parallel. Switching existing templates to `CONNECTION` continues after that.

### Phase 4: sunset (date to be agreed)

**Prerequisites**
- Every `call_execution_config` row is `CONNECTION`.
- No live CRM plan version reads `outcome`.
- Loom no longer reads the legacy fields.
- The CI allowlist is down to its minimum.
- #1207's judge takes the new fields as input.
- The announced date has passed.

**Steps, each released separately**
1. Announce: docs, changelog, email, and `Deprecation` / `Sunset` headers on the lead and analytics APIs.
2. Stop sending the `outcome` key in webhooks, APIs and CRM payloads, behind a global flag (reversible).
3. After one cycle, move the empty-outcome state checks (section 7) to `connection_status` / `agent_outcome`.
4. Delete the legacy code: the `BUSY` fallbacks, the `TRANSFERRED` and observer overrides, the `LEGACY` retry path, the legacy writes, and the coverage report.
5. Migration `082` (`record`): re-source the `crm_journey_event` view's `outcome` column (canon's 12 columns stay), so it no longer reads `lead_call_tracker.outcome`. Then migration `083` (Buddy): archive `(id, outcome)`, drop `lead_call_tracker.outcome`, `chat_session.outcome` and `idx_lead_call_tracker_outcome`, and add **format** CHECKs on the new columns (e.g. `^[A-Z0-9_]+$`). **This is the only irreversible step**; run it only after steps 2–4 have been stable for a cycle.
6. Remove the kill switches and the old analytics types.

**Effort:** backend 6–8 days, Loom 1–2 days; the notice period plus about 2 weeks.

### Effort summary

These figures assume one backend and one frontend engineer who know the codebase, and exclude the eval work (#1207) and review time.

| Phase | Backend | Frontend | Calendar |
|---|---|---|---|
| 1 | 10–12 d | 0.5 d | ~3 weeks (incl. 1-week soak) |
| 2 | 10–12 d | 1–2 d | ~2–3 weeks (incl. notice) |
| 3 | 12–15 d | 15–18 d | ~4 weeks in parallel |
| 4 | 6–8 d | 1–2 d | notice + ~2 weeks |
| **Total** | **~40–47 d** | **~17–20 d** | **~2–2.5 months to the end of Phase 3** |

## 6. Migrations

| # | Phase | File | Contents |
|---|---|---|---|
| 080 | 1 | `080_add_call_outcome_columns.sql` | All columns (both tables), `retry_policy`. No indexes |
| 081 | 2 | call outcome indexes | 3 partial indexes, built `CONCURRENTLY` by hand first, so the file is a no-op |
| 082 | 4 | `crm_journey_event` view | Re-source the view's `outcome` column so it stops reading `lead_call_tracker.outcome`, keeping canon's 12 columns (owner `record`; needs a canon check) |
| 083 | 4 | legacy drop | Archive, drop both `outcome` columns and their index, format CHECKs. Runs after 082 |

The journey view is not changed before the sunset: the journey read fetches the call outcome columns through a Buddy accessor (Phase 2, 2e).

080 is final. 081 onward are indicative: the next free number is taken at merge, and #1207 also needs one. No migration is needed to switch the eval on by default (that is owned by #1207) or to make `CONNECTION` the default retry policy (the creation code sets it).

## 7. Legacy readers to move before the sunset

| Area | Where | What it reads today |
|---|---|---|
| Retries | `managers/calls.py::handle_call_completion`, `handle_unanswered_calls`, reaper, reconcile | `outcome in ["BUSY", "NO_ANSWER"]` |
| State checks | `queries/.../lead_call_tracker.py::abort_lead_by_id_query` (`outcome IS NULL OR ''`), `managers/calls.py::reconcile_completed_call` (`claimed.outcome`), `agent/__init__.py` early hangup (`not lead.outcome`) | "empty outcome" meaning "no pipeline ran" |
| CRM | `_ANSWERED`, `crm/outreach/analytics.py`, `crm_mirror.py` (`call.completed.outcome`), view `crm_journey_event`, workflow plans' outcome branches | the legacy word |
| Analytics | `queries/breeze_buddy/analytics/analytics.py`, `api/routers/breeze_buddy/analytics/handlers.py` (substring matching, conversion funnel), `get_lead_based_analytics_query`, campaign `picked`, telephony-number `calls_no_answer` | various subsets |
| Webhooks | `callbacks/service_callback.py`, `_retry_call`, pre-check failure webhook | `outcome` key |
| Observability | `observability/tracing_setup.py` (`call_outcome`), Slack digest (`score_monitor/score.py`) | the legacy word |
| Evals | `services/conversation_analysis/worker.py` skip (`NO_ANSWER`, `VOICEMAIL`), #1207 `recorded_outcome` | the legacy word |
| Loom | the console lists and metrics in Phase 3, legacy dashboard, CSV export | `outcome`, `outcome_breakdown`, `outcome_counts` |

## 8. Chat

- `chat_session` gets the agent and eval columns.
- The outcome hook's chat branch writes `agent_outcome` / `outcome_source` behind the same switch.
- Chat has no connection layer; `ended_reason` (`user_ended`, `idle_timeout`) already covers how a session ended.
- There is no chat webhook today, so nothing needs to stay compatible.
- A chat outcome eval needs an engine that supports chat (Jev supports voice only).

## 9. Post-call eval (PR #1207)

The eval that fills `eval_*` is PR #1207 (`POST_CALL_QUALITY`, TypeSafe Jev engine). We create the columns and the PR fills them. We asked for:

1. **Sees the live outcome and adds to it.** `verified_outcome` receives `agent_outcome`, `outcome_source`, `metaData.outcome.*`, `connection_status` and `end_reason`, instead of "ignore recorded_outcome".
   - Fill when the live outcome is empty.
   - Keep it unless the transcript clearly contradicts it.
   - Otherwise suggest an outcome with a reason.
2. **Options from `template.configurations.outcomes`**, plus the reserved outcomes and `OTHER`. The `evaluation_config` criteria map is used only for templates without a list. Every option has a description.
3. **No `BUSY` / `NO_ANSWER` / `UNKNOWN` options** (they are connection or end-reason facts). Add `NONE`, stored as an empty `eval_outcome` with `eval_status=DONE`.
4. **Rewrite `outcome_correctness` and `latency`** against `agent_outcome` + `end_reason`, dropping the "busy is a technical fallback" rule. Prefer the new fields when present.
5. **On by default for every agent** with per-agent opt-out and a global kill switch. This conflicts with the PR's recorded "no global fallback" ruling and needs sign-off, together with sign-off on vendor cost and on sending data to the vendor at full volume.
6. **Write back to the lead** in the same transaction as the `evaluation_result` insert: `eval_outcome`, `eval_status=DONE`, `eval_result_id`. Only the outcome; scores stay in `evaluation_result`.
7. **`eval_status` lifecycle:**
   - `PENDING` when an eligible call is queued;
   - `SKIPPED` when a call isn't eligible;
   - `FAILED`, with a `FAILED` result row and `error_message`, after the last retry.
8. **Eligibility** by `connection_status = ANSWERED` when present, falling back to today's `NO_ANSWER` / `VOICEMAIL` skip. Skip `agent_outcome = VOICEMAIL`.
9. **Validate the output** against the allowed options. `OTHER` is stored as `OTHER`, with the suggested label in the metadata.
10. **Chat:** don't mark sessions `PENDING` until a chat engine exists.
11. **Fire the evaluated hook after writing `eval_*`**: once the write-back has committed, re-read the lead and call `lct_accessor.announce_call_evaluated(lead, notify_webhook)` (`accessor/breeze_buddy/lead_call_tracker.py`), where `notify_webhook` is the template's `evaluation_config.configuration.notify_webhook` (admin-only). Phase 2 already registers the CRM `call.outcome_evaluated` tap and the opt-in second webhook on it; the call is fail-open, so it never breaks the eval.
12. **Tests:** fill / agree / disagree, the transactional write-back, `eval_status` transitions, and eligibility with and without `connection_status`.
13. **Renumber** the PR's migration (073 is taken) and land the write-back after 080.

## 10. Open items

- **Sunset date and notice period.** To be agreed.
- **#1207 alignment.** Default-on vs the per-agent ruling, vendor cost, and sending data to the vendor at full volume.
- **Temporary-cause allowlist** for retrying `FAILED` under `CONNECTION`: build it from the `hangup_cause` values recorded during the soak (until then `FAILED` is final).
- **Existing issues found in the live run** (not caused by this work):
  - A dispatcher worker exception after `_acquire_number` (e.g. the provider client failing to build) never gives back the DB channel (`telephony_numbers.channels`). The number stays at capacity until fixed by hand.
  - `handle_call_completion` returns before its completion write when `_get_lead_config` finds nothing (e.g. a lead with no `template_id`), despite its comment "config lookup must not gate this". The lead stays `PROCESSING` until the reaper.
- **Out of scope, tracked separately:** `TODO.md` (Twilio's temporary "number in use" recorded as the permanent `NUMBER_UNAVAILABLE`), and retry leads not carrying `enrollment_id` (so a workflow hears only the first attempt).
