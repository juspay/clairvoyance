# Call Outcomes: Connection, Agent and Eval Layers

Status: **Phase 1 implemented** (branch `feat/call-outcome-columns`). Phases 2–4 planned.
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
| `EndReason` | `AGENT_ENDED`, `CUSTOMER_HANGUP`, `IDLE_TIMEOUT`, `TRANSFERRED`, `EARLY_HANGUP`, `IVR_NO_INPUT`, `IVR_ERROR`, `PIPELINE_ERROR`, `PIPELINE_NOT_STARTED` |
| `OutcomeSource` | `LLM`, `IVR`, `OBSERVER` |
| `EvalStatus` | `PENDING`, `DONE`, `FAILED`, `SKIPPED` |

**Reserved agent outcomes** are accepted by every template without being declared:
- `VOICEMAIL`: set by the voicemail observer or the eval. Once set it is never overwritten, and it is never retried.
- `CALLBACK_REQUESTED`: carries `metaData.outcome.callback_at`. The Phase 3 retry rules use it.

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
| `UNKNOWN` (completed, no pipeline) | `ANSWERED` | `PIPELINE_NOT_STARTED` | |
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
| 6 | Retries | Connection results (`NO_ANSWER`, line `BUSY`, temporary `FAILED`) plus `CALLBACK_REQUESTED` at `callback_at`. Per template (`retry_policy`); existing templates stay `LEGACY` until moved |
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
| Completed call with no pipeline | `reconcile_completed_call` | `ANSWERED` + `PIPELINE_NOT_STARTED` + `provider_status=completed` |
| Stuck-call reaper | `reconcile_stuck_processing_leads` | `ANSWERED` + `PIPELINE_ERROR` if a pipeline ran, else `UNKNOWN` |
| Pre-checks | `_run_pre_checks_for_lead` | `NOT_DIALED` + `PRECHECK_FAILED` |
| Merchant call cap | `managers/calls.py` call-limit refusal (`CALL_LIMIT_REACHED`) | `NOT_DIALED` + `CALL_LIMIT` |
| Dispatcher | `dispatch/worker.py` (`_fail_and_release`, blacklist) | `NOT_DIALED` + `NO_CONFIG` / `NUMBER_UNAVAILABLE` / `INVALID_PHONE` / `BLACKLISTED` |
| Aborts | `handle_lead_abort`, WooCommerce cancel, demo cleanup | `NOT_DIALED` + `ABORTED` |
| Inbound blocks | `services/inbound_policy.py`, `ivr/selection.py` | `REJECTED` + `BLOCKED` / `CAPACITY` |
| Ending paths | `agent/__init__.py` (idle timeout, disconnect, early hangup), `agent/utils.py::end_call_with_errors`, `end_conversation` | `end_reason`; fallback from `metaData.call_ended_by` |
| Outcome hook | `template/hooks.py::UpdateOutcomeInDatabaseHook` | `agent_outcome` + `outcome_source` (observer detected via `metaData.observer_triggered`), taken **before** the legacy transfer / observer overrides; an observer's outcome is not replaced by a later LLM call |
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

### Phase 2: backfill and additive exposure

**Backfill** (`scripts/backfill_call_outcomes.py`)
- Batches by id over `FINISHED` rows with an empty `connection_status` created before the Phase 1 cutover. It is idempotent, resumable, throttled, run off-peak, and stamps `backfilled_at`.
- One pure, unit-tested mapping function implements section 2.3.
  - Carrier `NO_ANSWER` stays `NO_ANSWER`, because busy/failed cannot be recovered: the raw status was never stored.
  - **`BUSY` is ambiguous.** It becomes an agent outcome only if the outcome hook left its trace (`metaData.outcome` present). Otherwise it is `ANSWERED`, with `end_reason` from `metaData.call_end_reason` / `call_ended_by`.
  - Rows it cannot classify get `UNKNOWN`.
- Review about 200 sampled `BUSY` rows manually before the full run.
- It never touches `outcome` or `metaData`.
- **Rollback:** set the columns back to NULL `WHERE backfilled_at IS NOT NULL`.

**Indexes** (before anything reads the columns)
- On production, build them by hand, outside a transaction:
  - `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lct_connection_status ON lead_call_tracker (connection_status) WHERE connection_status IS NOT NULL;`
  - the same for `agent_outcome` and `eval_outcome`.
- Then apply migration `081`, which holds the same three statements without `CONCURRENTLY`. `IF NOT EXISTS` makes it a no-op (the 054 precedent).

**Our read surfaces**
- The lead API already carries the fields (Phase 1). Call details and the CSV export (`custom_columns`) gain them too.
- **New** analytics types: a connection funnel (dialled → answered → reached a human → outcome decided → success), an outcome breakdown on `agent_outcome`, and the eval flag rate once #1207 fills `eval_*`. Existing analytics types keep returning exactly what they return today.
- Langfuse span attributes `connection_status`, `agent_outcome` alongside `call_outcome` (`observability/tracing_setup.py`).

**CRM**
- The `call.completed` payload (`crm_mirror.py::_finished_lead_tap`) gains `connection_status`, `connection_reason`, `end_reason`, `agent_outcome`, `outcome_source`. The `outcome` key is unchanged.
- New topic `call.outcome_evaluated`:
  - emitted through the accessor hook registry, because the data layer imports neither `app.ai` nor `app.crm`;
  - registered in the code catalog;
  - fired once #1207 writes `eval_*`.
- Migration `082`: `CREATE OR REPLACE VIEW crm_journey_event`, appending `connection_status`, `agent_outcome`, `eval_outcome`. It is owned by `record`, so it gets its own file.
- Existing workflow plans keep branching on `outcome` and are unaffected.

**Merchant webhooks**
- These builders gain `event`, `connectionStatus`, `connectionReason`, `endReason`, `agentOutcome`, `outcomeSource`, `evalOutcome: {status, value}`:
  - `callbacks/service_callback.py`;
  - the `NO_ANSWER` webhook in `managers/calls.py::_retry_call`;
  - the `PRECHECK_FAILED` webhook.
- Legacy keys, values and send conditions are unchanged, including the skip when a required schema field is missing.
- `evalOutcome` is null until #1207 lands, so merchants get one announcement for all the new keys.
- The keys sit behind the `WEBHOOK_CALL_OUTCOME_KEYS` dynamic flag.
- Order:
  1. Heads-up (changelog, docs, Nautilus team) N days ahead.
  2. Turn the flag on.
  3. Monitor **each merchant's non-2xx rate** before and after, and alert on an increase. That catches merchants who validate payloads strictly.
- **Opt-in second webhook:**
  - sent by the eval worker after `eval_status=DONE`, to the lead's `reporting_webhook_url`;
  - payload: the same legacy fields as the first webhook, plus `event: "call.outcome_evaluated"` and `evalOutcome`;
  - opt-in per template (`evaluation_config.configuration.notify_webhook`; see section 9 on who may set it).

**Docs:** in Loom (`src/docs/reference/webhooks.svx`, `rest/leads.svx`), document the new fields and mark `outcome` deprecated with the date to be announced.

**Done when:** the backfill is complete, and the webhook keys are on with no increase in merchant errors.

**Effort:** backend 10–12 days; about 2–3 weeks of calendar time including the notice.

### Phase 3: move our consumers over, connection-driven retries

**Loom**
- Conversations list and detail: separate **Connection** (status, reason, end reason), **Outcome** (agent outcome and source) and **Eval** (Filled / Confirmed / Flagged, shown only when present) sections. Filters on each. Remove the `outcome || status` fallbacks.
- Remove the mixed hardcoded lists:
  - `NON_OUTCOME_KEYS` (`types/analytics.ts`)
  - the `outcomeBadge` pattern matching (`console/agent-charts.ts`)
  - substring rules (`console/voice-metrics.ts`)
  - `DashboardMetrics`
  - the campaign stack bar
  - `wf/perf/metrics.ts` labels
  - `wf/catalog.svelte.ts` fallbacks
- Metrics: show the new definitions **alongside** the old ones, labelled, for one release cycle, then swap. Include an in-app note, since merchants will see numbers shift.
- Template builder:
  - an outcome-list editor (`configurations.outcomes`);
  - dropdowns for hook static outcome values, IVR option outcomes and observer outcomes.
- Agent settings: the eval toggle and the second-webhook opt-in.
- Workflow editor: a "Connected / No answer / Busy / Failed / Not dialled" branch on `connection_status`, and an outcome branch on `call.outcome_evaluated`.

**Template outcome lists**
- `ConfigurationModel.outcomes: Optional[List[OutcomeDefinition]]` with `name`, `description` and `is_success`. Descriptions are required, because PR #1163's eval found that options without descriptions collapse.
- When a template has a list, it is checked on save: hook static values, IVR options and observer outcomes must be in the list or the reserved set.
- Templates with a list get the allowed values as a fixed set on the `update_outcome` function and the outcome hooks.
- "Required for new templates" is enforced in Loom's create flow now. The public template API enforces it only on the Phase 4 date, so API clients that create templates don't break.
- Generator prompts (`template/generator/prompts.py`) produce a list, stop suggesting `BUSY` / `NO_ANSWER` as agent outcomes, and add `CALLBACK_REQUESTED`.

**Connection-driven retries** (`managers/calls.py`)
- `retry_policy` values: `LEGACY` or `CONNECTION`. The code reads `if config.retry_policy == "CONNECTION": new rules; else: today's code, untouched`.
- `CONNECTION` re-dials on:
  - `NO_ANSWER`;
  - line `BUSY`;
  - `FAILED` when `hangup_cause` is on a temporary-cause allowlist (never an invalid number).
- `CALLBACK_REQUESTED` re-dials at `metaData.outcome.callback_at`, kept inside calling hours and a maximum horizon, and counts toward `max_retry`.
- `VOICEMAIL`, deliberate skips and other agent outcomes are final.
- Rollout:
  1. Internal templates first.
  2. The code that creates new configs sets `CONNECTION`; the DB default stays `LEGACY`, so no migration is needed.
  3. Existing configs switch one at a time with the merchant's agreement, because call volume changes.

**Internal reports**
- Add a `_REACHED` definition (answered, and not `VOICEMAIL`) next to `_ANSWERED` in `queries/breeze_buddy/lead_call_tracker.py`.
- Switch `crm/outreach/analytics.py` over and update `tests/crm/test_console_reads.py`. Remove the old definition after Loom's swap.
- Switch the Slack digest (`services/langfuse/tasks/score_monitor/score.py`) to the new definitions.
- Publish new versions of the cart-recovery plans (`docs/crm/plans/cart-recovery-*.json`) that branch on `connection_status`. Runs already in progress finish on their old version.

**CI rule:** no new reads of `lead_call_tracker.outcome` / `chat_session.outcome` outside an allowlist:
- the legacy webhook and API builders;
- the `LEGACY` retry path;
- the empty-outcome state checks (section 7).

Following repo practice it ships as a triple: docs text, the rule in `scripts/`, and a failing test.

**Done when:** the CI allowlist holds only those items.

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
5. Migration `083` (Buddy): archive `(id, outcome)`, drop `lead_call_tracker.outcome`, `chat_session.outcome` and `idx_lead_call_tracker_outcome`, and add **format** CHECKs on the new columns (e.g. `^[A-Z0-9_]+$`). Migration `084` (`record`): recreate `crm_journey_event` without `outcome`. **This is the only irreversible step**; run it only after steps 2–4 have been stable for a cycle.
6. Remove the kill switches and the old analytics types.

**Effort:** backend 6–8 days, Loom 1–2 days; the notice period plus about 2 weeks.

### Effort summary

These figures assume one backend and one frontend engineer who know the codebase, and exclude the eval work (#1207) and review time.

| Phase | Backend | Frontend | Calendar |
|---|---|---|---|
| 1 | 10–12 d | 0.5 d | ~3 weeks (incl. 1-week soak) |
| 2 | 10–12 d | — | ~2–3 weeks (incl. notice) |
| 3 | 12–15 d | 15–18 d | ~4 weeks in parallel |
| 4 | 6–8 d | 1–2 d | notice + ~2 weeks |
| **Total** | **~40–47 d** | **~17–20 d** | **~2–2.5 months to the end of Phase 3** |

## 6. Migrations

| # | Phase | File | Contents |
|---|---|---|---|
| 080 | 1 | `080_add_call_outcome_columns.sql` | All columns (both tables), `retry_policy`. No indexes |
| 081 | 2 | call outcome indexes | 3 partial indexes, built `CONCURRENTLY` by hand first, so the file is a no-op |
| 082 | 2 | `crm_journey_event` view | Append 3 columns (owner `record`) |
| 083 | 4 | legacy drop | Archive, drop both `outcome` columns and their index, format CHECKs |
| 084 | 4 | `crm_journey_event` view | Recreate without `outcome` (owner `record`) |

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
11. **Call a hook after writing `eval_*`** (accessor hook registry). We wire the CRM `call.outcome_evaluated` event and the opt-in second webhook to it.
12. **Tests:** fill / agree / disagree, the transactional write-back, `eval_status` transitions, and eligibility with and without `connection_status`.
13. **Renumber** the PR's migration (073 is taken) and land the write-back after 080.

## 10. Open items

- **Sunset date and notice period.** To be agreed.
- **#1207 alignment.** Default-on vs the per-agent ruling, vendor cost, and sending data to the vendor at full volume.
- **Second-webhook opt-in.** In #1207's API only admins can edit an eval's configuration. Decide whether merchants can opt in themselves or ops does it.
- **Temporary-cause allowlist** for retrying `FAILED` (Phase 3), per provider (`hangup_cause` values).
- **Existing issues found in the live run** (not caused by this work):
  - A dispatcher worker exception after `_acquire_number` (e.g. the provider client failing to build) never gives back the DB channel (`telephony_numbers.channels`). The number stays at capacity until fixed by hand.
  - `handle_call_completion` returns before its completion write when `_get_lead_config` finds nothing (e.g. a lead with no `template_id`), despite its comment "config lookup must not gate this". The lead stays `PROCESSING` until the reaper.
- **Out of scope, tracked separately:** `TODO.md` (Twilio's temporary "number in use" recorded as the permanent `NUMBER_UNAVAILABLE`), and retry leads not carrying `enrollment_id` (so a workflow hears only the first attempt).
