# Template Playbook — what a "correct" Breeze Buddy template looks like

> Companion to `docs/blueprint/TEMPLATE_CREATION_AGENT.md`. That doc describes how the Blueprint agent is wired; this doc describes the *output* the agent must produce. Source of the 24-point silent-breakage checklist consumed by `app/ai/text/agents/blueprint/specialists/template_linter.py` (Part 4).

A template is a contract between **three parties**:

1. The **runtime pipeline** (Pipecat) — needs voice/STT/TTS settings and a flow graph it can execute.
2. The **lead payload** — per-call data the merchant sends; reachable via `{placeholder}` in prompts.
3. The **outcome consumer** (Postgres + service callbacks + analytics) — needs structured outcome tags and optional LLM-extracted fields.

Most "broken" templates fail at the seam between two of these — e.g. a prompt references `{loan_amount}` but `expected_payload_schema` doesn't declare it, so the runtime renders an empty string and the agent says "Your loan amount of  is overdue." A correct template gets every seam right on the first build.

---

## Part 1 — Build order (steps must run in this sequence)

Each step's output is consumed by later steps. Reordering breaks seams.

### Step 1 — Identity & ownership

| Field | Role | Mandatory? |
|---|---|---|
| `name` | Display + lookup | ✅ |
| `reseller_id` | Tenant boundary; log context | ✅ (set on session creation, never asked) |
| `merchant_id` | Sub-tenant scope | Only when known |
| `identifier` | Merchant-facing slug | Optional |
| `is_active` | Cron skips inactive templates | ✅ (default `true`) |
| `outbound_number_id` | FK to outbound phone number | ✅ for outbound; null for inbound-only |

### Step 2 — Direction & inbound policy

Decide **before configuring anything else** whether the template is inbound, outbound, or both. This single answer determines:

* Whether `enable_inbound: true` and `ivr_configuration` are needed.
* Whether `outbound_number_id` is required.
* Whether warm transfer is meaningful (mostly outbound).

### Step 3 — `expected_payload_schema`

The **most-skipped, most-load-bearing** field. Without it, every `{placeholder}` in prompts becomes an empty string at runtime.

Common pattern from production templates: `customer_name` (almost always) plus 0–6 use-case fields. Loan collection: `customer_name, loan_amount, due_date, loan_id`. Order confirmation: `customer_name, shop_name, total_price, items, customer_address, customer_mobile_number`. Numeric amounts spoken aloud should set `function: indian_number_to_speech` so the TTS renders "two lakh fifty thousand" instead of "250000".

This step **must** complete before flow / prompt design — those steps reference these placeholders.

### Step 4 — `expected_callback_response_schema`

Documents the shape of completion webhooks the merchant receives. Optional but strongly recommended; consumed by the webhook code, not the voice pipeline. For loan collection: `{ payment_commitment: { type: string, optional: true }, callback_date: { type: string, optional: true } }`.

### Step 5 — Conversational config (`configurations.*`)

Order matters within this group; some choices constrain others.

1. **STT provider + language** — `soniox` is the safe default (English + Hindi + code-switching native). `deepgram` if English-only and you want SmartTurn. `sarvam` for Indic-only flows.
2. **Turn detection** — `stt_native` for Soniox / Deepgram (their native endpointing is best); `smart_turn` only with Deepgram + the SmartTurn ML model; `timeout` as a fallback.
3. **TTS provider + voice** — `elevenlabs` default; `cartesia` for sub-200ms latency; `sarvam` for Indic voices. Set `voice_id` explicitly when known.
4. **Initial greeting** — capture as the user typed it, with `{customer_name}` etc. inline. The runtime substitutes against the payload.
5. **Interruption** — `mode=enabled, min_words=3` is the human-feel default. `min_words` filters "hmm" / "yeah" backchannels.
6. **User idle** — `enabled=true, timeout=8, max_retries=2` is industry norm. Provide an `idle_message` that names the merchant ("Are you still there? This is Rhea from HSBC").
7. **VAD** — only relevant when `turn_detection=smart_turn`. Skip otherwise.
8. **Audio extras** — `keyword_filter` with filler-word list (`hmm`, `yes`, `okay`, `acha`, `haan`, `ji`) to prevent false interruptions; `noise_filter={enable:true, type:aic}` for noisy lines.
9. **Background sound** — only if requested; if `enable=true`, **must** also set `background_sound_file: office-ambience`.
10. **LLM provider** — `azure` default.

### Step 6 — Conditional configs (run only when triggered)

* **Warm transfer**: if any flow function dispatches `connect_to_live_agent`, `configurations.transfer_number` is mandatory.
* **IVR**: if `enable_inbound=true` AND the template is an IVR menu, populate `ivr_configuration` with `greeting`, `goodbye`, `priority>=1`, optional `tts_configuration` override.
* **Background sound**: see above.
* **Secrets**: only when an HTTP global function references `{api_key}`-like placeholders that aren't in the payload.

### Step 7 — Flow design (`flow.nodes`)

Read the entire transcript. Identify:

* **Merchant** + **agent persona** ("Rhea from HSBC collections")
* **Call objective** (collect overdue loan repayment)
* **Branches** the agent must handle (paid, dispute, callback, wrong person, voicemail, busy)
* **Terminal outcomes** for each branch

Emit a node graph where:

* **Initial node** is the entry point — typically `greet_and_check_availability` or `verify_identity` for outbound.
* **Domain nodes** are scenario-specific (`verify_identity`, `present_loan_details`, `negotiate_payment`, `handle_dispute`, `arrange_followup`).
* **Terminal node** is single, named `end_conversation_node`, with:
  * `pre_actions: [{type: function, handler: mute_stt}]`
  * `post_actions: [{type: function, handler: end_conversation}]`
  * Outcome-switched goodbye in `task_messages`.
* Every domain function that completes a branch transitions to `end_conversation_node`.

### Step 8 — Functions + outcome hooks

For every function that ends a branch (terminal or branching), attach:

```json
{
  "name": "user_busy",
  "description": "Customer is unavailable to talk now.",
  "properties": {},
  "required": [],
  "transition_to": "end_conversation_node",
  "hooks": [{
    "name": "update_outcome_in_database",
    "expected_fields": {
      "outcome": { "source": "static", "value": "BUSY" }
    }
  }]
}
```

Standard outcome vocabulary (SCREAMING_SNAKE_CASE; never lowercase):

| Outcome | When it fires |
|---|---|
| `BUSY` | Customer can't talk / wrong person / voicemail |
| `<DOMAIN_SUCCESS>` | Happy-path completion (e.g. `CONFIRM`, `PAYMENT_COMMITTED`, `FEEDBACK_COLLECTED`) |
| `<DOMAIN_REFUSAL>` | Customer explicitly declined (`CANCEL`, `DISPUTE_UNRESOLVED`) |
| `CALLBACK_REQUESTED` | Reschedule / call later |
| `TRANSFERRED` | Auto-set when warm transfer succeeds — never write manually |

For functions where the LLM extracts data (cancellation reason, payment date, rating), add the same hook with both `outcome` and the extracted fields:

```json
{
  "name": "negotiate_payment",
  "properties": {
    "payment_date": { "type": "string", "description": "ISO date the customer commits to paying." },
    "payment_amount": { "type": "number" }
  },
  "required": ["payment_date"],
  "hooks": [{
    "name": "update_outcome_in_database",
    "expected_fields": {
      "outcome":         { "source": "static", "value": "PAYMENT_COMMITTED" },
      "payment_date":    { "source": "llm",    "value": "payment_date" },
      "payment_amount":  { "source": "llm",    "value": "payment_amount" }
    }
  }]
}
```

### Step 9 — Global functions

Almost every template has one builtin: `end_conversation`. Add others on demand:

* **`connect_to_live_agent`** when warm transfer is needed. Set `pre_tts_message: "Connecting you to a live agent now, please hold."` so the farewell isn't clipped. Requires `transfer_number` (Step 6).
* **`get_current_time`** when the agent needs to reason about the current time (rare).
* **`update_outcome`** as a fire-and-forget outcome write without a transition.

HTTP global functions: only when the agent must call an external service mid-call (refund, balance lookup). Specify full `http_request` with `auth`, `timeout`, `max_retries`. Reference secrets via `{api_base_url}` placeholders.

### Step 10 — Persona + prompts

For every node:

* **`role_messages`** carry persistent persona ("You are Rhea, a collections specialist for HSBC. Speak professionally, switch to Hindi if the customer prefers, never reveal you're a bot."). These persist across transitions.
* **`task_messages`** carry node-specific instruction ("Greet using `{customer_name}`, then ask if this is a good time."). Re-injected on every node entry.

Both support `{placeholder}` substitution (and ONLY these — function descriptions, properties, action text are NOT substituted).

### Step 11 — Flow-level extras

* **`flow.initial_node`** — must exactly match a `node_name` in the active set.
* **`flow.end_conversation_callbacks: ["service_callback"]`** — required to fire the outcome webhook to the merchant. Currently the only registered callback name; anything else is silently dropped.

### Step 12 — Validate before assembly

Cross-check the draft against the **silent-breakage checklist** (Part 4) before calling `ReplaceTemplateRequest.model_validate()`. Pydantic only catches type mismatches, not contract violations.

### Step 13 — Assemble + finalize

Round-trip through `ReplaceTemplateRequest.model_validate()`. On success, persist `template_json`. On Pydantic failure, surface errors and retry once with corrections (capped at 1 auto-retry by `state.finalize_retries`).

---

## Part 2 — Compact field reference

Required field × default × silent-break trigger × when-to-set.

| Field | Type | Default | Silent-break if missing | Set it when |
|---|---|---|---|---|
| `name` | str | — | Hard fail | Always |
| `is_active` | bool | true | Pydantic error | Always |
| `reseller_id` | str | — | Hard fail | On session creation |
| `outbound_number_id` | str | None | Outbound dial fails | Outbound templates |
| `expected_payload_schema` | dict | None | `{var}` rendered as empty/literal | When prompts use placeholders (almost always) |
| `expected_callback_response_schema` | dict | None | Webhooks have undocumented shape | When the outcome should be sent to merchant |
| `configurations.tts_configuration.provider` | enum | None | TTS subprocess crashes | Always |
| `configurations.stt_configuration.provider` | enum | soniox | Falls back to soniox | Almost always |
| `configurations.initial_greeting` | str | None | Agent says nothing on connect | Always |
| `configurations.transfer_number` | str | None | Warm transfer silently fails | When flow uses `connect_to_live_agent` |
| `configurations.background_sound_file` | enum | None | No sound plays | When `enable_background_sound=true` |
| `configurations.user_idle_configuration.enabled` | bool | false | Stalled calls never end | Almost always set true |
| `configurations.keyword_filter.keywords` | list | [] | Backchannel "hmm" interrupts | Recommended for outbound |
| `flow.initial_node` | str | — | Build fails | Always |
| `flow.nodes[*].functions[*].transition_to` | str | None | LLM stuck on node forever | When function should advance the conversation |
| `flow.nodes[*].functions[*].hooks` | list | [] | Outcome never recorded | On terminal/branch-ending functions |
| `flow.global_functions[*].handler` | str | — | Unknown handler error | Must exactly match: `connect_to_live_agent`, `end_conversation`, `get_current_time`, `update_outcome` |
| `flow.end_conversation_callbacks` | list | None | Merchant webhooks never fire | When merchant has a callback URL |

---

## Part 3 — Built-in handlers + numeric ranges

### Built-in handler registry

| Handler key | Description | Requirements |
|---|---|---|
| `connect_to_live_agent` | Warm transfer to human agent | `configurations.transfer_number` must be set |
| `end_conversation` | End the call (pushes EndFrame) | None |
| `get_current_time` | Returns current time in IST | None |
| `update_outcome` | Updates call outcome in database | None |
| `mute_stt` | Mutes STT/VAD (prevents transcription) | None |
| `unmute_stt` | Unmutes STT/VAD (resumes transcription) | None |
| `play_audio_sound` | Plays an audio file | Audio file reference in args |

### Action handlers for `pre_actions` / `post_actions`

| Handler | Usage |
|---|---|
| `mute_stt` | Mute speech-to-text during bot speech |
| `unmute_stt` | Resume speech-to-text after bot speech |
| `end_conversation` | Terminate the call |
| `play_audio_sound` | Play background audio |
| `connect_to_live_agent` | Transfer call |

### Numeric ranges quick reference

| Field | Min | Max | Default |
|---|---|---|---|
| `VadConfig.confidence` | 0.0 | 1.0 | — |
| `VadConfig.start_secs` | 0.0 | — | — |
| `VadConfig.stop_secs` | 0.0 | — | — |
| `VadConfig.min_volume` | 0.0 | — | — |
| `CartesiaVoice.volume` | 0.5 | 2.0 | — |
| `CartesiaVoice.speed` | 0.6 | 1.5 | — |
| `ElevenLabsVoice.speed` | 0.7 | 1.2 | — |
| `LLM.temperature` | 0.0 | 2.0 | — |
| `LLM.max_tokens` | 1 | — | — |
| `Thinking.budget_tokens` | 1024 | — | — |
| `SmartTurn.stop_secs` | 0.0 | — | 3.0 |
| `SmartTurn.pre_speech_ms` | 0.0 | — | 500.0 |
| `SmartTurn.max_duration_secs` | 1.0 | — | 8.0 |
| `SmartTurn.cpu_count` | 1 | — | 1 |
| `Deepgram.utterance_end_ms` | 1000 | — | null |
| `Deepgram.endpointing_ms` | — | — | 25 |
| `STT.user_speech_timeout` | 0.0 | — | 0.3 |
| `background_sound_volume` | — | — | 2.0 |
| `InterruptionConfig.min_words` | 1 | — | null |
| `UserIdle.timeout` | 0.0 | — | 5.0 |
| `UserIdle.max_retries` | 1 | — | 3 |
| `IVR.ivr_priority` | 1 | — | null |
| `HttpRequest.timeout` | — | — | 10 |
| `HttpRequest.max_retries` | — | — | 3 |
| `InputCollection.user_speech_timeout` | 0.0 | — | 0.0 |

### Top-level template shape

```json
{
  "reseller_id": "string (required)",
  "name": "string (required)",
  "merchant_id": "string or null",
  "outbound_number_id": "string or null",
  "is_active": true,
  "flow": {
    "initial_node": "string (required)",
    "end_conversation_callbacks": ["service_callback"],
    "global_functions": [],
    "nodes": []
  },
  "expected_payload_schema": {},
  "expected_callback_response_schema": {},
  "configurations": {},
  "secrets": {}
}
```

---

## Part 4 — Silent-Breakage Checklist (24 traps)

The Blueprint generator must defend against every one. Pydantic catches none of these. Each entry is implemented as an auto-fix or a warning in `app/ai/text/agents/blueprint/specialists/template_linter.py`.

1. `{placeholder}` in prompts not declared in `expected_payload_schema` → literal stays in output.
2. `{placeholder}` in `functions[].description` / `properties[].description` / action text → never substituted.
3. Payload schema declares a field the merchant doesn't send → empty string in greeting.
4. Unknown transformation function name in payload schema → silently skipped.
5. `initial_node` points to inactive or missing node → build fails (hard).
6. `transition_to` points to non-existent node → LLM loops on current node.
7. Global function with no `type` and no `http_request` → dropped at registration.
8. Builtin `handler` not in `{connect_to_live_agent, end_conversation, get_current_time, update_outcome}` → unknown handler at call time.
9. Warm transfer flow but `transfer_number` unset → silent fail.
10. `update_outcome_in_database` hook attached but `outcome` field absent → outcome never written.
11. `send_http_request` hook with missing `http_request` → no-op.
12. `end_conversation_callbacks` references anything other than `service_callback` → silently skipped.
13. TTS provider whose API key isn't configured → subprocess crash at bring-up.
14. `turn_detection=smart_turn` with non-Deepgram provider at wrong sample rate → trigger gating misfires.
15. IVR option references missing or `enable_inbound=false` child template → DTMF selection fails.
16. `ivr_configuration` set but `enable_inbound=false` → IVR never fires.
17. `pre_tts_message` on a non-terminal builtin → STT muted unnecessarily; UX confusion.
18. LLM-source `expected_fields` names a key not in `function.properties` → field omitted from outcome.
19. Outcome miscased (`transferred` lowercase) → analytics double-counts.
20. Whitespace / non-identifier in placeholder (`{ order id }`) → silent miss.
21. Function name collides between node and global function → undefined which is called.
22. `is_playground=true` accidentally on production payload → configs clobbered.
23. `user_speech_timeout` set but `turn_detection != timeout` → silently zeroed.
24. `background_sound_file` missing when `enable_background_sound=true` → no sound plays.
