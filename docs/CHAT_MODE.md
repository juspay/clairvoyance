# Breeze Buddy Chat Mode — Design & Implementation Plan

Status: **Implemented (direct LLM driver, post-pipeline rewrite).**
Owners: Breeze Buddy team.
Related docs: [BREEZE_BUDDY_ARCHITECTURE.md](./BREEZE_BUDDY_ARCHITECTURE.md), [DAILY_RTVI_EVENTS.md](./DAILY_RTVI_EVENTS.md), [README_WARM_TRANSFER.md](./README_WARM_TRANSFER.md).

> **2026-05-04 update — pipeline removed.** Chat mode no longer runs through a Pipecat
> `Pipeline` / `PipelineRunner` / `FlowManager` / custom transport. The driver now calls
> the provider SDK directly and reuses pipecat's `LLMAdapter` + `LLMContext` only as
> stateless schema/format helpers. Sections §4, §7, §9 reflect the direct driver. D3 in
> §3 is preserved as historical context — it is no longer the operative decision.

---

## 1. Problem Statement

Today Breeze Buddy supports template-driven conversational AI agents only over **voice** channels (Twilio/Plivo/Exotel telephony, Daily web). The same template (nodes, functions, hooks, LLM prompts) cannot be exposed as a **text/chat** experience. We want to extend Breeze Buddy so that a single template can power both voice and text/chat agents, with text mode preserving the entire flow and LLM behavior while removing STT/TTS/VAD.

## 2. Goals & Non-Goals

### Goals (v1)
- Expose a Breeze Buddy template as an inbound, web-only, text-based chat agent.
- Reuse the existing template, FlowManager, LLM, global functions, and handlers without forking the execution path.
- REST + SSE API surface, JWT/RBAC scoped by reseller/merchant (same model as voice).
- Resumable sessions with full message history.
- Auto-greeting, idle timeout, configurable token streaming (block or stream).
- Surface RTVI-style events to the chat client (function-call "thinking…", flow transitions, end-of-turn).

### Non-Goals (v1)
- Async/messaging channels (WhatsApp, SMS, Slack) — architecture extendable, not implemented.
- Outbound chat (proactive first-message) — architecture extendable, not implemented.
- Warm transfer to a human chat agent — deferred to **Phase 2**. The `warm_transfer` global function will be **hidden from the LLM** in chat-mode templates for v1.
- Frontend UI inside clairvoyance — we expose the backend; loom owns the UI.
- Cross-channel transfer (chat ↔ voice).

## 3. Decisions (Locked In)

| # | Decision | Rationale |
|---|---|---|
| D1 | New `chat_session` + `chat_message` tables, **not** a reuse of `LeadCallTracker`. | Lifecycle is fundamentally different (inbound, on-demand, resumable; no cron, no dial retry). Voice-specific columns don't fit. Mirror the **outcome contract** so webhooks/analytics see uniform "interaction" records. |
| D2 | **Append-only message log** + lightweight session snapshot. No "rewrite full state blob per turn." | O(1) write per turn vs O(N²) bytes over a session. Scales to 10K+ concurrent sessions on modest Postgres. Cheap rehydration via indexed range scan. |
| D3 | ~~Keep using **Pipecat pipeline + FlowManager** even for text.~~ **Superseded 2026-05-04.** Chat now uses a **direct LLM driver** (provider SDK directly + pipecat adapters as stateless helpers). Voice still uses the full pipeline. | Pipeline/runner/transport/FlowManager were paying the voice-architecture tax — frame ordering races, drain-loop counters, `on_pipeline_started` handshakes, 60s frame-timeout guard — for a code path that just streams tokens and dispatches tool calls. Direct driver removed ~600 LOC of scaffolding while keeping templates, hooks, and node transitions intact. See §4 + §9. |
| D4 | **Single-process, asyncio-task-per-session.** No subprocess pool. | Chat is pure async I/O. Voice's subprocess model is justified by audio CPU work (VAD/STT codec); text doesn't need it. One process can serve hundreds-thousands of concurrent chats. |
| D5 | **Explicit opt-in** via `supported_channels` array on the template (default `["voice"]`). | Most existing voice templates have voice-coded phrasing ("press 1", "you can now speak"). Silent dual-mode would surprise authors and pollute analytics. |
| D6 | Voice-only handlers (`play_audio_sound`, `mute_stt`, `unmute_stt`) are **filtered out of functions and pre/post actions** when the builder is constructed with `disabled_names=CHAT_DISABLED_NAMES`. | Single `handler_map` for both channels; LLM never sees voice-only functions; no risk of an action-handler running against a transport that has no STT or audio. |
| D7 | `warm_transfer` is **hidden from the LLM** when running a chat session in v1. Implemented in Phase 2. | Real text-to-text human handoff needs a human-agent inbox UI + identity model + relay; meaningful sub-project. |
| D8 | REST + SSE transport. **Pattern A**: one SSE stream per turn (POST returns SSE). | Simpler than long-lived SSE; matches REST semantics; resumable across reconnects. |
| D9 | Token streaming is **always on** in v1 — every turn emits `assistant_token` deltas before the final `assistant_message`. The per-session `stream: true \| false` opt-out is deferred; clients that want block mode can ignore the deltas. | Cuts a request-shape decision out of the v1 surface; we can add the opt-out without a breaking change later. |
| D10 | RBAC: same JWT model as `/api/breeze_buddy/*` routers, scoped by reseller_id/merchant_id. | No new auth model. |
| D11 | In-flight turn commit: **commit-on-completion** (assistant turn persists only after fully generated). | Simpler than streaming-with-incremental-commit; users see clean history on resume. Streaming-with-commit can come in a later phase. |
| D12 | Per-session **distributed lock** (Redis) on POST to prevent concurrent-turn races across pods. **Fail-fast: 409 Conflict if held** — no server-side queueing, no blocking acquire. | No sticky-session requirement; any pod can serve any session. Blocking acquire would tie up FastAPI workers and a TCP keep-alive slot per waiting client; clients that need serialized requests can chain on the previous response client-side. |

## 4. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         FastAPI Router                              │
│  POST /chat/session                                                 │
│  GET  /chat/session/{id}                                            │
│  POST /chat/session/{id}/message  → SSE stream (one turn)           │
│  POST /chat/session/{id}/end                                        │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │ per-session Redis lock
                                  ▼
                ┌──────────────────────────────────────┐
                │             ChatAgent                │
                │   (constructed-and-discarded /turn)  │
                │                                      │
                │   ┌──────────────────────────────┐   │
                │   │ build flow_config (template) │   │
                │   │ wrap handlers w/ context     │   │
                │   │ seed LLMContext from history │   │
                │   └─────────────┬────────────────┘   │
                │                 │                    │
                │                 ▼                    │
                │   ┌──────────────────────────────┐   │
                │   │   chat/llm_driver.stream()   │   │
                │   │                              │   │
                │   │  dispatch by service type:   │   │
                │   │    - AzureLLMService    →    │   │
                │   │      OpenAI streaming        │   │
                │   │    - VertexAnthropic    →    │   │
                │   │      messages.create stream  │   │
                │   │    - GoogleVertex (Gemini)→  │   │
                │   │      generate_content_stream │   │
                │   │                              │   │
                │   │  yields: ("text", str)       │   │
                │   │          ("tool_call", call) │   │
                │   └─────────────┬────────────────┘   │
                │                 │                    │
                │                 ▼                    │
                │   on tool calls: invoke handler      │
                │     (transition_handler etc.)        │
                │     → if returns next_node_config,   │
                │       swap node task_messages,       │
                │       loop back to driver            │
                │   on no-tool turn: turn complete     │
                └─────────────┬────────────────────────┘
                              ▼
                ┌──────────────────────────────┐
                │   PostgreSQL                 │
                │   chat_session  (state row)  │
                │   chat_message  (append log) │
                └──────────────────────────────┘
```

**Key idea:** chat is a thin loop around a streaming LLM call. Pipecat's adapters
(`OpenAILLMAdapter` / `AnthropicLLMAdapter` / `GeminiLLMAdapter`) and `LLMContext` are
imported as stateless helpers — schema conversion and message-format conversion only.
There is no `Pipeline`, `PipelineRunner`, `FlowManager`, custom transport, or frame
classifier in the chat code path.

### What's reused vs new vs deleted (post-rewrite)

| Component | Action |
|---|---|
| `agents/breeze_buddy/template/{types,builder,transition,hooks,loader,context}.py` | **Reused unchanged.** Template models, FlowConfigBuilder, transition_handler, HookRegistry, render_messages_with_vars, TemplateContext (+ `with_context`) all work outside the pipeline because they are pure async functions. |
| `agents/breeze_buddy/handlers/**` | **Reused unchanged.** Every handler signature already takes `(context, args, ...)` — chat just invokes them directly instead of via a frame-driven function-call registry. |
| Pipecat `LLMAdapter` (per provider) | **Reused as a stateless helper.** Driver calls `service.get_llm_adapter().get_llm_invocation_params(context, ...)` to convert universal-shaped messages + tools into the provider's wire format. |
| Pipecat `LLMContext` | **Reused as a data container.** No aggregator, no observers — just a place to hold the running message list and tools. |
| Pipecat provider service (`AzureLLMService`, `GoogleVertexLLMService`, `VertexAnthropicLLMService`) | **Reused for credentials + client construction only.** Driver reads `service._client` (the AsyncOpenAI / AsyncAnthropicVertex / genai client) and `service._settings` (model, max_tokens, temperature, thinking, …) and issues the streaming SDK call directly. |
| `agents/breeze_buddy/agent/pipeline.py` `build_chat_pipeline()` | **Deleted.** Voice-only `build_pipeline()` stays. |
| `agents/breeze_buddy/agent/flow.py` `setup_flow_manager()` | **Not called from chat.** Voice still uses it. |
| `agents/breeze_buddy/chat/text_transport.py` | **Deleted.** No transport, no frame plumbing. |
| `agents/breeze_buddy/chat/sse.py` `classify_frame` / `TURN_END` | **Deleted.** Driver yields SSE events directly; classifier indirection gone. `SSEEvent` + `format_sse` retained. |
| `agents/breeze_buddy/chat/agent.py` | **Rewritten.** ~150 LOC: build flow_config → seed LLMContext → loop {stream LLM → dispatch tool calls → maybe transition → repeat}. |
| New: `agents/breeze_buddy/chat/llm_driver.py` | Provider-agnostic streaming + tool-call accumulator. Three async generators, one per wire format (OpenAI-compatible / Anthropic / Gemini). |
| `agents/breeze_buddy/chat/cleanup.py` | **Reused unchanged.** Idle-session sweeper (one-shot DB UPDATE; no pipeline involvement). |
| `database/migrations/026_*.sql`, `database/{queries,accessor,decoder}/breeze_buddy/chat_session.py`, `schemas/breeze_buddy/chat.py`, `api/routers/breeze_buddy/chat/{handlers,rbac}.py` | **Reused unchanged.** None of these touch the pipeline or driver internals. |

## 5. Data Model

### 5.1 `chat_session`

Session-level state. One row per chat session.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Session ID exposed to clients. |
| `template_id` | UUID FK → `template.id` | Which template drives the conversation. |
| `reseller_id` | varchar(255) | RBAC scoping. Matches existing voice-table type. |
| `merchant_id` | varchar(255) NULL | RBAC scoping. |
| `status` | varchar(20) | `ACTIVE` \| `IDLE` \| `ENDED`. (No `BACKLOG`/`PROCESSING` — that's voice-only semantics.) |
| `outcome` | varchar(50) NULL | Reuses voice outcome enum for analytics uniformity. |
| `current_node` | varchar(255) NULL | FlowManager's current node name. Indexed-by-name analytics + cheap O(1) restore on rehydration. |
| `metadata` | JSONB | Session-creation payload (resolved `template_vars`, `request_id`, etc.) plus any sticky flags hooks set during the conversation (e.g., `transfer_initiated`). |
| `created_at` | TIMESTAMPTZ | |
| `last_activity_at` | TIMESTAMPTZ | Updated on every turn. Drives idle eviction + idle-timeout end. |
| `ended_at` | TIMESTAMPTZ NULL | |
| `ended_reason` | varchar(50) NULL | `user_ended` \| `idle_timeout`. |

> No `channel` column or `FAILED` status: there's only one channel today (web), and failure paths emit an SSE `error` event without mutating the session row. Add real channel discrimination back when WhatsApp/SMS/Slack land.

**Why no `template_version` column:** v1 doesn't pin templates per session — sessions read the latest template from DB on every rehydration (same as voice). Add real version-pinning when the `template` table itself gets versioning.

**Why no `flow_state_json` column:** the per-session runtime state we'd put in it is already covered: `current_node` is its own indexed column; resolved `template_vars` live in `metadata`; tool-call replay across turns is owned by the in-process `LLMContext` (chat agents are constructed-and-discarded per turn but the context is rebuilt from message history); sticky hook flags live in `metadata`. Add it back only if FlowManager turns out to have non-recoverable internal state we need to round-trip.

Indexes:
- `(reseller_id, merchant_id, status, last_activity_at DESC)` for dashboard listing.
- `(status, last_activity_at)` for the idle-timeout sweeper job.
- `(template_id)` for analytics by template.

### 5.2 `chat_message` (append-only)

| Column | Type | Notes |
|---|---|---|
| `session_id` | UUID FK → `chat_session.id` | |
| `idx` | INT | Monotonic per session. |
| `role` | TEXT | `user` \| `assistant`. |
| `content` | TEXT NULL | The message text. |
| `created_at` | TIMESTAMPTZ | |

Primary key: `(session_id, idx)`. Index: same. Insert-only — never updated.

> No `function_call_json` / `function_response_json` / `tokens_in` / `tokens_out` / `latency_ms` columns and no `system` / `function` role values: tool calls collapse into the trailing assistant text and are replayed per turn from the rebuilt `LLMContext`, and there's no per-call instrumentation pass yet. Reintroduce in lockstep with the writers.

**Rehydration query:**
```sql
SELECT role, content
  FROM chat_message
 WHERE session_id = $1
 ORDER BY idx ASC;
```

### 5.3 Migration `026_create_chat_session_tables.sql`

Single forward migration adding both tables + indexes. No backfill (chat is a new product). Follows the [migration policy](../CLAUDE.md): never edit existing migration files; create new sequential ones.

### 5.4 Outcome contract uniformity

`chat_session.outcome`, `chat_session.metadata`, and the end-of-session webhook payload mirror the voice equivalents on `LeadCallTracker`. A merchant integrating with both channels sees the same JSON shape with a `channel` discriminator.

## 6. API Surface (REST + SSE)

All endpoints under `/api/breeze_buddy/chat/`. JWT required; same RBAC middleware as existing breeze_buddy routers.

### 6.1 Endpoint Inventory

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/session` | Create new chat session for a template. Returns session ID + initial assistant greeting. |
| `GET` | `/session/{id}` | Resume — returns full message history and current status. |
| `POST` | `/session/{id}/message` | Send user message. Returns **SSE stream** for the assistant's turn. |
| `POST` | `/session/{id}/end` | Explicit end (user closes the chat). Fires outcome webhook. |
| `GET` | `/session/{id}/transcript` | Plain JSON transcript export (read-only). |

### 6.2 `POST /session`

Request:
```json
{
  "template_id": "uuid",
  "channel": "web",
  "template_vars": { "customer_name": "Asha", ... },
  "metadata": { "anything": "useful" },
  "stream": true
}
```

Response (200):
```json
{
  "session_id": "uuid",
  "status": "ACTIVE",
  "greeting": { "role": "assistant", "content": "Hi Asha, ..." },
  "current_node": "intro"
}
```

Validation:
- Template must exist and have `"chat" in supported_channels`.
- Reseller/merchant scope check.

Side effects:
- INSERT `chat_session`.
- If the template has `configurations.initial_greeting`, render it with
  `template_vars` and INSERT the first `chat_message` row (`role=assistant`).
  No agent / pipeline is constructed at session-create time — chat is
  stateless-per-turn (§4) and the first `POST /message` builds the
  per-turn `ChatAgent` on demand.

### 6.3 `GET /session/{id}`

Response:
```json
{
  "session_id": "uuid",
  "status": "ACTIVE | IDLE | ENDED",
  "current_node": "verify_identity",
  "messages": [
    { "idx": 0, "role": "assistant", "content": "Hi Asha…", "created_at": "..." },
    { "idx": 1, "role": "user", "content": "Hello", "created_at": "..." },
    ...
  ],
  "metadata": { ... }
}
```

If `IDLE`: the session is rehydratable. Sending a `/message` will revive it.
If `ENDED`: read-only.

### 6.4 `POST /session/{id}/message`  → SSE

Request:
```json
{ "content": "user typed text" }
```

Acquires per-session lock (Redis). Returns **SSE stream** with these event types:

| Event | Payload | Notes |
|---|---|---|
| `user_committed` | `{ idx, content }` | The user message has been persisted. |
| `assistant_token` | `{ delta }` | Token stream (v1 always streams; the per-session `stream` opt-out from D9 was deferred). |
| `assistant_message` | `{ idx, content }` | Full assistant turn (always emitted at end of turn, after streaming). |
| `function_call_started` | `{ name, args, tool_call_id }` | "Thinking…" indicator equivalent. |
| `function_call_completed` | `{ name, tool_call_id, result_summary }` | |
| `node_transition` | `{ to }` | Synthesized node moved (flow mode only). The `from` field from D8's draft was dropped — clients should track `current_node` themselves between turns. |
| `turn_end` | `{ session_status }` | Closes the SSE stream. Always last. |
| `error` | `{ code, message }` | Stream closes after. |

If lock contended → HTTP 409 (don't open SSE).
If session ENDED → HTTP 410 Gone.

### 6.5 `POST /session/{id}/end`

Marks `ENDED` under the per-session lock. The outcome webhook (same shape as
voice end-of-call webhook with `channel: "web"`) is a phase-2 follow-up — it
isn't fired by v1. There is no in-memory registry to evict from (§4).

### 6.6 `GET /session/{id}/transcript`

Plain JSON. Useful for export/audit. Same payload as `GET /session/{id}` minus runtime status.

## 7. Agent Runtime

### 7.1 `ChatAgent` (`app/ai/voice/agents/breeze_buddy/chat/agent.py`)

**One-shot per-turn driver.** Fresh `ChatAgent` per `POST /message`; single user → assistant
turn; discarded. No long-lived state, no pipeline, no runner.

```
class ChatAgent:
    session_id, template, template_vars, llm_service

    async def run_turn(
        *, user_content: str,
        history: list[dict],         # prior turns from DB
        current_node: str | None,    # resume node from chat_session.current_node
    ) -> AsyncIterator[SSEEvent]
```

`run_turn` is a single async loop:

1. Build `flow_config` via `FlowConfigBuilder(disabled_names=CHAT_DISABLED_NAMES).build_flow_config(template)`.
2. Wrap each entry in `flow_builder.handler_map` with `with_context(self)` so the
   `transition_handler` and friends receive a `TemplateContext` first arg.
3. Resolve the resume node: `current_node` or `initial_node`.
4. Render `role_messages` + `task_messages` via `render_messages_with_vars` and
   `inject_language_rules` (chat path mirrors voice's `FlowConfigLoader.load_template`).
5. Persist the user message → yield `user_committed`.
6. Build an `LLMContext`:
   - `messages = [system role_messages] + task_messages + history + [user_content]`
   - `tools = ToolsSchema(standard_tools=[fn.to_function_schema() for fn in node.functions])`
7. **Tool-call loop:**

   ```python
   while True:
       text_chunks: list[str] = []
       tool_calls: list[FunctionCallFromLLM] = []
       async for kind, payload in llm_driver.stream(self._llm, context, label):
           if kind == "text":
               text_chunks.append(payload)
               yield SSEEvent("assistant_token", {"delta": payload})
           elif kind == "tool_call":
               tool_calls.append(payload)
               yield SSEEvent("function_call_started", {...})

       if not tool_calls:
           break  # turn complete

       # Mirror what an LLMAssistantContextAggregator would write back:
       context.add_message({"role": "assistant", "tool_calls": [...]})
       for call in tool_calls:
           result, next_node_cfg = await invoke_handler(call, flow_config)
           context.add_message({"role": "tool",
                                "tool_call_id": call.tool_call_id,
                                "content": json.dumps(result)})
           yield SSEEvent("function_call_completed", {...})
           if next_node_cfg is not None:
               # FlowManager-style transition: swap task_messages + tools
               apply_node_transition(context, next_node_cfg, template_vars)
   ```

8. Persist the assistant message → yield `assistant_message`.
9. Single combined UPDATE on `chat_session` (last_activity_at + current_node).

There is no `try / finally` around a pipeline runner anymore — there is nothing to
cancel. The only cleanup is closing whatever the SDK opened, which the SDK handles on
its own when the async generator returns.

### 7.2 `chat/llm_driver.py` — provider-agnostic streaming

Single module. Public surface:

```python
async def stream(
    llm_service: AzureLLMService | GoogleVertexLLMService | VertexAnthropicLLMService,
    context: LLMContext,
    *,
    log_label: str,
) -> AsyncIterator[tuple[Literal["text", "tool_call"], str | FunctionCallFromLLM]]:
    """Issue one streaming LLM call; yield text deltas and (after stream ends)
    any tool calls. Provider dispatch happens inside on the service type."""
```

Internally three implementations, each ~50 LOC, port the streaming +
tool-call accumulation from pipecat's per-provider `_process_context` minus the
frame-pushing layer:

| Service class | SDK call | Streaming pattern |
|---|---|---|
| `AzureLLMService` (any `BaseOpenAILLMService` subclass) | `service._client.chat.completions.create(stream=True, **params)` | OpenAI delta-stream: `tool_calls[idx].function.{name,arguments}` accumulate per index; flush on index change. (Mirrors `pipecat/services/openai/base_llm.py:404-540`.) |
| `VertexAnthropicLLMService` (any `AnthropicLLMService` subclass) | `service._client.beta.messages.create(stream=True, **params)` | Anthropic event stream: `content_block_start (tool_use)` → `content_block_delta (partial_json)` accumulate; flush on `message_delta(stop_reason="tool_use")`. (Mirrors `pipecat/services/anthropic/llm.py:340-487`.) |
| `GoogleVertexLLMService` (any `GoogleLLMService` subclass) | `service._client.aio.models.generate_content_stream(...)` | Gemini parts: `part.text` for content, `part.function_call` already-materialized for tool calls. (Mirrors `pipecat/services/google/llm.py:432-619`.) |

**How the driver talks to the service:**
- `adapter = service.get_llm_adapter()` (already attached by pipecat at construction).
- `params_from_context = adapter.get_llm_invocation_params(context, ...)` returns
  provider-shaped `messages`/`tools`/`system` dict.
- `service._settings` provides model, max_tokens, temperature, top_p, thinking, etc.
- `service._client` is the SDK client pipecat already constructed (with our
  Azure/Vertex/AnthropicVertex credentials from `app/ai/voice/llm/*.py`).

Reusing pipecat's adapter means OpenAI-compatible providers we don't currently use
(DeepSeek, Groq, Cerebras, Mistral, Fireworks, Ollama, Nvidia, Nebius, Perplexity,
Grok) automatically work behind the OpenAI branch as soon as someone wires them into
`get_llm_service`. Anthropic-direct (non-Vertex) and Google Gemini API (non-Vertex)
also work behind their respective branches with no driver change.

### 7.3 Idle session cleanup (`app/ai/voice/agents/breeze_buddy/chat/cleanup.py`)

Unchanged from the pipeline-era design. There is no in-memory registry; the only
background task is `end_idle_chat_sessions()` registered with the global
`BackgroundTaskScheduler`. The scheduler's distributed Redis lock keeps the sweep
single-pod-per-tick.

### 7.4 Per-turn lifecycle

1. Acquire per-session Redis lock (TTL 180s).
2. Re-read `chat_session` under the lock.
3. Read capped message history.
4. Read template.
5. Build a fresh `ChatAgent` with the configured LLM service; `run_turn(...)`.
6. Stream SSE events to the client until `turn_end` or `error`.
7. Release the lock in `finally`.

Per-turn cost (excluding LLM): ~3 indexed DB reads + 2 INSERTs + 1 UPDATE. **Pipeline
construction cost is now zero** (no `PipelineTask`, no `PipelineRunner`, no transport).

### 7.3 Idle session cleanup (`app/ai/voice/agents/breeze_buddy/chat/cleanup.py`)

There is **no in-memory registry** in chat mode (decision recorded in §11). The only background concern is preventing zombie ACTIVE/IDLE rows from accumulating when users walk away.

`end_idle_chat_sessions()` is a single async function registered against the global `BackgroundTaskScheduler` from `app/main.py` lifespan:

```
_background_scheduler.register_task(
    name="chat_session_idle_cleanup",
    func=end_idle_chat_sessions,
    interval_seconds=CHAT_SESSION_END_TIMEOUT_LOOP_INTERVAL_SECONDS,  # static, default 300s
)
```

The scheduler's distributed Redis lock guarantees only one pod runs the sweep per interval — at 100s of pods that's a single global cron, not N redundant sweeps.

The function:
1. Reads `CHAT_SESSION_END_TIMEOUT_SECONDS` from dynamic config (live-tunable via DevCycle).
2. Queries `list_idle_chat_sessions(cutoff, [ACTIVE, IDLE])`.
3. For each row, calls `end_chat_session(..., ended_reason=IDLE_TIMEOUT)`. The accessor's UPDATE is idempotent (`WHERE status <> 'ENDED'`), so duplicate sweeps from a botched lock acquire would still be safe.

### 7.4 Per-turn lifecycle (no rehydration step)

In the stateless model there is no separate "cache hit" vs "cache miss" path. Every `POST /message` runs the same sequence:

1. Acquire per-session Redis lock (TTL 180s, no auto-extend).
2. Re-read `chat_session` under the lock (covers the race where another pod ended it between request entry and lock acquisition).
3. Read `chat_message` rows for the session — these become `prefill_messages` for `LLMContext`.
4. Read the template (single indexed query).
5. Build a fresh `ChatAgent`, call `run_turn(user_content, history, current_node)`.
6. Stream SSE events to the client until `turn_end`.
7. Release the lock in `finally`.

Per-turn cost (excluding the LLM call itself): ~3 indexed DB reads + 2 INSERTs + pipeline construction. Pipeline construction in chat mode is light — no STT, TTS, VAD, smart-turn, or Daily-room provisioning. Single-digit ms for the build.

## 8. Template Layer Changes

### 8.1 `supported_channels` flag

Add to `template/types.py`:

```
class TemplateModel(BaseModel):
    ...
    supported_channels: List[Literal["voice", "chat"]] = Field(
        default_factory=lambda: ["voice"]
    )
```

- Default `["voice"]` keeps every existing template safe (chat creation rejects them).
- Authors opt in by adding `"chat"`.
- Validation: at least one channel; values constrained to the literal set.
- **Applies to both `flow` and `direct` template modes.** The field lives on `TemplateModel` (sibling to `flow`), so it is mode-agnostic. A direct-mode template with `supported_channels: ["voice", "chat"]` builds in chat just like a flow-mode one — the only difference is which build path inside `FlowConfigBuilder` is taken (flow vs direct), and the chat-mode function/action strip (see §8.2) applies uniformly to both paths.

Builder/dashboard UX: a single channel-checkbox group in the template editor. Out of scope for this doc — backend provides the field; dashboard work is its own ticket.

### 8.2 Voice-only feature handling in chat mode

`FlowConfigBuilder` accepts a `disabled_names: AbstractSet[str] = frozenset()` kwarg — chat mode is realised by *filtering* template references to voice-only handlers, not by swapping the handlers for noops. The set itself lives in `app/ai/voice/agents/breeze_buddy/chat/disabled.py` (`CHAT_DISABLED_NAMES`); the builder is channel-agnostic and just consumes it. Voice callers pass nothing (default empty set, no-op fast-path); chat callers pass `CHAT_DISABLED_NAMES`:

```
mute_stt
unmute_stt
play_audio_sound
warm_transfer
connect_to_live_agent
end_conversation
```

A single module-level helper `filter_disabled_identifiers(items, disabled, log_label)` does the actual filtering — it drops any dict whose identifier (resolved via `name > function_name > handler`) is in the disabled set. When `disabled` is empty (voice mode) it returns the input list unchanged with no copy, so the same call site is used regardless of caller.

The filter is applied at four sites before Pydantic validation:

1. **Per-node `functions`** (flow mode) — LLM never sees disabled functions as callable.
2. **Per-node `pre_actions` / `post_actions`** (flow mode) — `type == "function"` actions whose `handler` is disabled are dropped; `tts_say` and other non-function actions pass through untouched, so the chat pipeline never tries to invoke STT-mute / audio-play against a transport that has neither.
3. **Top-level `global_functions`** (flow mode).
4. **Flat `functions` array** (direct mode).

A `WARNING` log line is emitted for every stripped entry, suffixed with the call-site label (`function` or `action`) so authors can locate the affected JSON array.

Non-voice handlers (`end_conversation`, `transition_handler`, `http_function_handler`, `builtin_function_dispatcher`, `custom_python_code_handler`) work unchanged across both channels.

### 8.3 Identifier precedence

The filter resolves a dict's identifier via `name > function_name > handler`. A template that explicitly renames a function (e.g., `name: "transfer_to_finance"` with `handler: "connect_to_live_agent"`) is treated as the renamed function and **passes through** — the filter only fires when the user-visible identifier itself is in `CHAT_DISABLED_NAMES`. This is intentional: a renamed function probably points to bespoke logic the template author wants to keep, and the precedence makes that opt-out free.

Action dicts only carry a `handler` key, so the chain reduces to that key naturally — no separate code path is needed for actions.

`warm_transfer` / `connect_to_live_agent` are v1 stopgap entries in `CHAT_DISABLED_NAMES` — Phase 2 replaces these with a real chat-to-human handoff (see §15).

### 8.4 Initial greeting

**Default: voice-equivalent (LLM-generated).** Reuse `prepare_initial_node` (`agent/flow.py:134-182`). After cold-start, run one synthetic LLM turn with **no user message** to elicit the greeting from the template's `task_messages` and template_vars. This matches voice behavior exactly.

**Optional override: `static_greeting` field on the template.** If the template defines a non-empty `static_greeting` string (`{placeholder}` substitution applied from `template_vars`), skip the LLM call and use that string directly. Saves one LLM call per session start — meaningful at high-volume merchants where the greeting is the same every time anyway.

In both paths:
- Persist the greeting as the first `chat_message` (`role=assistant`, `idx=0`).
- Return it in the `POST /session` response.

### 8.5 Validation rules at session creation

When `POST /session` is called:
1. Template must include `"chat"` in `supported_channels`.
2. Template's required `template_vars` must be satisfied by the request payload (same logic voice uses).
3. Realtime LLM models (S2S) — disallow in v1; chat templates must use a text LLM. Return 400.

## 9. Direct LLM Driver (replaces "Pipecat Pipeline in Text Mode")

### 9.1 Why we dropped the pipeline

The voice pipeline is justified by what it abstracts: VAD, STT, TTS, turn detection,
interruption strategies, transcription gate, audio mixers, RTVI observer, OTEL tracing.
For chat *none of those exist*; what survives is one LLM call + a tool-call dispatch.
Running that through `Pipeline → PipelineRunner → BreezeBuddyTextTransport →
LLMUserAggregator → LLMService(FlowManager-driven) → LLMAssistantAggregator →
BreezeBuddyTextTransport.output → SSE classifier` paid the voice tax for no benefit
and surfaced as several real bugs: frame-ordering races between SystemFrame fast-path
and ControlFrame queueing, `on_pipeline_started` handshake required before pushing
user text, drain-loop counters to detect "this LLM cycle vs the next", a 60s
per-frame timeout that triggered when streaming was actually fine.

### 9.2 What replaces it

Three pieces:

1. **`chat/llm_driver.py`** — three async generators (one per wire format) issuing the
   provider-specific streaming SDK call directly. Each yields `("text", str)` for
   content deltas and `("tool_call", FunctionCallFromLLM)` for materialized tool calls
   after the stream closes. ~150 LOC total.

2. **`chat/agent.py`** — one async loop:
   build flow_config → seed `LLMContext` → repeat { stream LLM → if tool calls,
   dispatch handlers + apply transitions → continue; else break }. ~150 LOC.

3. **Pipecat `LLMAdapter` per provider** — pure schema/format converters. Driver calls
   `adapter.get_llm_invocation_params(context, ...)` to get the provider-shaped
   `messages`/`tools`/`system` dict, then merges with `service._settings` to build the
   request params.

### 9.3 Per-turn flow

```
POST /message
  │
  ▼
agent.run_turn(user_content, history, current_node)
  ├── build flow_config (FlowConfigBuilder, disabled_names=CHAT_DISABLED_NAMES)
  ├── wrap handler_map with with_context(self)
  ├── render role_messages + task_messages w/ template_vars
  ├── seed LLMContext = role + task + history + user
  └── loop:
        │
        ▼
        llm_driver.stream(llm_service, context)
          ├── get adapter via service.get_llm_adapter()
          ├── adapter.get_llm_invocation_params(context) → provider-shaped params
          ├── service._client.<provider-call>(stream=True, **params)
          └── async for chunk:
                ├── text delta  → yield ("text", "hello")
                └── tool delta  → accumulate in indexed buffer
              when stream ends:
                └── yield ("tool_call", FunctionCallFromLLM(...))
        │
        ▼
        agent collects:
          ├── append text deltas → SSE assistant_token (streamed)
          └── if tool_calls:
                ├── add assistant message w/ tool_calls to LLMContext
                ├── for each call: invoke handler (the wrapper from
                │     FlowConfigBuilder._build_function_schema —
                │     handler returns (FlowResult, NodeConfig | None))
                ├── add tool result message(s) to LLMContext
                ├── if NodeConfig returned: swap task_messages + tools
                │     (apply_node_transition, mirroring FlowManager._set_node)
                └── continue loop
        │
        ▼
        else (no tool calls):
          ├── persist assistant message → SSE assistant_message
          ├── update chat_session.last_activity_at + current_node
          └── SSE turn_end
```

### 9.4 Provider portability matrix

| Provider | Used today by Buddy | Driver branch | LOC for new wire format |
|---|---|---|---|
| Azure OpenAI | ✅ default | OpenAI-compatible | 0 |
| Google Vertex (Gemini) | ✅ template opt-in | Gemini | 0 |
| Anthropic on Vertex (Claude) | ✅ template opt-in | Anthropic | 0 |
| Anthropic direct (non-Vertex) | future | reuse Anthropic branch | 0 |
| Google Gemini API (non-Vertex) | future | reuse Gemini branch | 0 |
| OpenAI direct, DeepSeek, Groq, Cerebras, Mistral, Fireworks, Ollama, Nvidia, Nebius, Perplexity, Grok | future | reuse OpenAI branch | 0 |
| New non-OpenAI-compatible provider | future | new branch | ~50 LOC |

### 9.5 RTVI events for chat

**Removed from chat in v1.** The RTVI observer was attached to the (now-deleted)
pipeline. Function-call lifecycle events that loom cares about are still surfaced
as first-class SSE events (`function_call_started`, `function_call_completed`,
`assistant_token`). Re-introducing RTVI envelope semantics for chat is a future
refinement; track in CHAT_MODE follow-ups if loom asks for it.

## 10. Persistence & Rehydration

### 10.1 Append-only message log

Per turn:
- 1 INSERT: user message → `chat_message` (`role=user`).
- 1 INSERT: assistant message → `chat_message` (`role=assistant`).
- 1 UPDATE on `chat_session.last_activity_at` (and `current_node` only when transition occurred).

Tool-call rounds inside one turn don't write extra rows — tool calls fold into the trailing assistant text and are replayed each turn from the rebuilt `LLMContext`. If structured tool-call persistence becomes a requirement (audit, replay across processes), reintroduce `function_call_json` / `function_response_json` together with the writers.

### 10.2 Snapshot triggers

Two pieces of session state get updated outside the per-turn message inserts:

- `current_node` (top-level column) — `UPDATE chat_session SET current_node = $1` whenever FlowManager transitions. Typically rare relative to message turns.
- `metadata` (JSONB) — `UPDATE chat_session SET metadata = $1` only when a hook sets a sticky flag (e.g., `transfer_initiated`). Most turns leave `metadata` untouched.

`last_activity_at` is updated on every turn in the same transaction as the `chat_message` inserts.

### 10.3 In-flight turn handling

- Assistant message persists **only after the full turn completes** (even when streaming tokens to client).
- If client disconnects mid-stream:
  - Pipeline continues to completion (don't abort the LLM call mid-stream — wasteful and risks half-applied function calls).
  - Final assistant message + any function calls still persist.
  - On reconnect, the user sees the complete assistant message in history.
- If pod crashes mid-stream: the user's message has already been persisted (committed before LLM started). The assistant's response is lost. On reconnect, the user sees their message but no assistant reply. They can resend or rephrase. (Acceptable for v1; Phase 4 may add streaming-with-incremental-commit.)

### 10.4 Crash recovery

On pod startup:
- No "drain" step required — sessions live in DB and there is no in-memory cache to warm.
- The global `BackgroundTaskScheduler` runs `end_idle_chat_sessions` (§7.3) on its own cadence; any row left ACTIVE/IDLE past `CHAT_SESSION_END_TIMEOUT_SECONDS` is marked ENDED with `ended_reason=idle_timeout`.

## 11. Concurrency & Multi-Pod Safety

### 11.1 Per-session lock

Redis lock keyed by `session_id`, acquired at the start of `POST /message` and `POST /end`, released on completion. TTL of 180s — comfortably longer than any reasonable single-turn LLM call (function-calling chain, tail latencies). **No auto-extend.** A turn that runs longer than the TTL means the upstream is hung; the lock should expire so retries can recover, not perpetually block all other pods.

If lock contended → 409 Conflict. Client retries.

**Implementation:** `RedisLock` async context manager in `app/services/redis/locks.py`:

- `acquire(key, ttl)` — `SET NX EX`, returns a unique token (so concurrent holders can't release each other's locks).
- `release(key, token)` — compare-and-del Lua script.

The scheduler can adopt this helper later; no immediate change there.

### 11.2 No sticky sessions, no in-memory cache

Any pod can serve any session because all state lives in DB + the per-session Redis lock. There is no per-pod in-memory `ChatAgent` map — the agent is constructed fresh per turn. This trades a few ms of pipeline construction for:
- linear horizontal scale (no cache-coherence cost across N pods),
- no phantom warm agents on pods that previously served a session,
- no background eviction loop running redundantly on every pod.

The decision is recorded in §15 Phase 1 → see also CLAUDE.md "Chat (text) mode".

### 11.3 No in-process lock

Because the agent is one-shot per turn there is no shared in-pod state to guard. The Redis lock alone is the mutual-exclusion primitive.

## 12. Observability

- **OTEL spans:** root span per chat session, child span per turn (mirroring voice). Span attributes: `chat.session_id`, `chat.template_id`, `chat.merchant_id`, `chat.current_node`, `chat.turn_idx`.
- **Logging:** extend existing `set_log_context` / `update_log_context` to include `chat_session_id`. All chat code paths participate in the same loguru context vars used by voice.
- **Langfuse:** existing tracing pipeline captures LLM calls regardless of channel. Add `channel: "chat"` as a span attribute so traces are filterable.
- **Metrics:** per-pod counters for active sessions, idle evictions, rehydrations, lock contentions, turn latency p50/p95.

## 13. Loom client-sdk Wiring

Backend exposes the API; loom owns the client. Out of scope for this repo but the contract is:

- New `ChatClient` class in `loom/packages/client-sdk` analogous to the Daily voice client.
- Methods: `createSession`, `sendMessage` (returns async iterator over SSE events), `resume`, `end`, `getTranscript`.
- Reuses RTVI event types where applicable so app-level event handlers can be shared between voice and chat.
- This work is tracked in a separate ticket against the `loom` repo.

## 14. Testing Strategy

### 14.1 Unit tests
- `BreezeBuddyTextTransport` frame plumbing (push text → frame appears in queue, output frames fan out to subscribers).
- `ChatSessionRegistry` idle eviction.
- Chat-mode strip: `_strip_chat_disabled_functions` and `_strip_chat_disabled_actions` remove voice-only entries (per §8.2) for both flow and direct templates, while leaving voice-mode builds untouched.
- Template `supported_channels` validation.
- `flow_state_json` round-trip (snapshot + rehydrate yields equivalent FlowManager state).

### 14.2 Integration tests
- End-to-end: create session → greeting → 5 user messages crossing 2 node transitions → end → verify webhook fired with correct outcome and full transcript.
- Resume: create session → 3 turns → simulate eviction → 2 more turns → verify history is correct and FlowManager continues from the right node.
- Concurrency: parallel POSTs to same session → exactly one succeeds, others get 409.
- Voice-only action in chat: template with `play_audio_sound` action runs without error, no audio side-effect.
- Realtime LLM template rejected at chat session creation (400).

### 14.3 Manual smoke
- Run a real template end-to-end against a local dev server using `curl` for POSTs and `curl -N` for SSE.
- Run the same template in voice mode (existing path) afterward to confirm no regression.

## 15. Phased Delivery

### Phase 1 (v1) — MVP
**Scope:** everything in this doc except warm transfer.

Deliverables:
- Migration 026.
- DB layer: queries/accessor/decoder for chat tables.
- `app/ai/voice/agents/breeze_buddy/chat/` module: `agent.py`, `text_transport.py`, `registry.py`, `sse.py`.
- Adapter in `agent/pipeline.py`: `build_chat_pipeline()`.
- `template/types.py`: `supported_channels` field; `template/builder.py`: chat-mode handler override map; `warm_transfer` strip.
- Router: `api/routers/breeze_buddy/chat.py` with the 5 endpoints.
- Schemas: `schemas/breeze_buddy/chat.py`.
- Outcome webhook reuse with `channel: "web"`.
- Tests per §14.
- Doc: this file kept current; update `BREEZE_BUDDY_ARCHITECTURE.md` with a chat-mode pointer.

**Exit criteria:** A merchant can author a template with `supported_channels: ["voice", "chat"]`, create a chat session via API, hold a multi-turn conversation with token streaming and function calls, disconnect and resume, and receive an outcome webhook on session end.

### Phase 2 — Warm Transfer to Human Chat Agent
**Scope:** real text-to-text human handoff.

Deliverables:
- Human-agent identity model (which clairvoyance users are "chat agents," presence/availability).
- Inbox API: list pending transfer requests, accept, decline.
- Relay: post-handoff messages route between user and human; the LLM steps out (or stays as copilot — design decision in Phase 2 kickoff).
- New session lifecycle states: `TRANSFER_PENDING`, `TRANSFERRED`.
- `warm_transfer` handler implemented for chat (currently stripped).
- Loom UI: agent inbox + chat handoff UX.

**Open design questions for Phase 2:**
- AI as silent observer post-handoff vs full disengagement?
- Routing rules (round-robin, skill-based, last-touched-by)?
- Concurrent agent capacity limits?

### Phase 3 — Additional Channels
**Scope:** WhatsApp first (most-requested), then SMS/Slack as needed.

What changes:
- Channel-specific webhook receivers (incoming message → push to existing chat infrastructure).
- Channel-specific outbound senders (WhatsApp Business API / Twilio Messaging API / Slack API).
- Outbound chat (proactive first-message) becomes a real requirement here.
- Async semantics: a "session" may span days; idle timeout becomes much longer; in-memory registry becomes mostly cache, DB becomes canonical.
- Per-channel formatting (markdown vs WhatsApp-flavored).

### Phase 4 — Performance & Scale
Triggers (any of these):
- Sustained 5K+ concurrent active sessions per pod.
- Postgres write IOPS becomes a bottleneck.
- Azure connection-rate limits or ephemeral-port exhaustion appear in logs.
- p99 turn latency creeps up and correlates with TLS handshake counts.
- Users complain about LLM-stream interruption losing context.

Options to deploy:
- **Shared `httpx.AsyncClient` for the Azure LLM path.** Each chat turn currently constructs a fresh `AsyncAzureOpenAI` → fresh `httpx.AsyncClient` → fresh TCP+TLS on first request. Sharing a process-wide client across turns saves the 50–200ms handshake on subsequent turns when the pool stays warm (idle timeout ~60–120s upstream) and reduces ephemeral-port / connection churn against the Azure endpoint. Implementation: subclass `AzureLLMService`, override `create_client` to pass `http_client=` into `AsyncAzureOpenAI`, cache the httpx client by `(endpoint, api_version)` in a process-wide dict, close on lifespan shutdown. Roughly ~80 LOC for Azure-only; per-provider abstraction (Vertex / Claude) when those land in chat. Skipped in Phase 1 — measure before building. **Note**: pipecat's `AzureLLMService.create_client` ignores `**kwargs` so injection requires the subclass; out-of-the-box the kwarg won't reach `AsyncAzureOpenAI`.
- **Redis hot cache** for `chat_message` (write-through). Reduces Postgres read load on rehydration.
- **Streaming-with-incremental-commit:** persist assistant tokens as they stream so a mid-stream disconnect leaves a partial-but-recoverable transcript.
- **Per-session sharding** of the registry across pods (consistent hashing) to reduce rehydration churn.
- **Message-log windowing:** load only the last N messages + a running summary for long sessions. Trivial query change; no schema migration. *(Shipped in Phase 1 as a hard cap via `CHAT_HISTORY_REPLAY_LIMIT`; the "running summary" half remains future work.)*

## 16. File Inventory (current state — post pipeline-removal rewrite)

Chat-specific code (`app/ai/voice/agents/breeze_buddy/chat/`):
```
__init__.py        # package marker
agent.py           # ChatAgent.run_turn — single async loop; ~150 LOC
llm_driver.py      # provider-agnostic streaming + tool-call accumulator
sse.py             # SSEEvent dataclass + format_sse wire helper (no frame classifier)
cleanup.py         # idle session sweeper (registered with BackgroundTaskScheduler)
```

Deleted in the rewrite:
```
text_transport.py                            # BreezeBuddyTextTransport, _TextInputProcessor, _TextOutputProcessor
agent/pipeline.py::build_chat_pipeline()     # text-only pipeline branch
chat/sse.py::classify_frame, TURN_END        # frame → SSE classifier (driver yields events directly)
```

Shared template / handler code (unchanged — works in both voice and chat unmodified):
```
agents/breeze_buddy/template/{types,builder,transition,hooks,loader,context,vad,interruption,input_collection}.py
agents/breeze_buddy/handlers/internal/*.py
agents/breeze_buddy/handlers/transport/http_handler.py
agents/breeze_buddy/template/global_function.py
```

Router / DB / schemas (unchanged):
```
api/routers/breeze_buddy/chat/{__init__,handlers,rbac}.py
schemas/breeze_buddy/chat.py
database/migrations/026_create_chat_session_tables.sql
database/{queries,accessor,decoder}/breeze_buddy/chat_session.py
services/redis/locks.py
```

Deferred (still tracked, unaffected by the rewrite):
- LLM-generated greeting at cold-start (only `static_greeting` works today)
- Outcome webhook on session end
- RTVI envelope events for chat (if loom asks for them)
- Per-turn LLM metrics (tokens, latency) and structured tool-call persistence — add columns + writers in lockstep when an instrumentation pass lands.

Out-of-repo (loom):
```
loom/packages/client-sdk/src/chat/ChatClient.ts
loom/packages/client-sdk/src/chat/types.ts
```

## 17. Resolutions to Pre-Coding Open Questions

| # | Question | Resolution |
|---|---|---|
| Q1 | ~~Pipecat `BaseTransport` interface + cleanest input frame~~ | **Obsolete after 2026-05-04 rewrite.** Chat no longer uses a transport, an aggregator, or any frame plumbing. The driver pushes user text into an `LLMContext` directly and streams the response from the SDK. See §7.2 + §9. |
| Q2 | Greeting cost | **Both, with voice-equivalent default.** LLM-generated greeting (current voice behavior) is the default. Optional `static_greeting` field on the template overrides when present, saving one LLM call per session start. See §8.4. |
| Q3 | Idle TTLs | **Inactivity-based, configurable via dynamic config.** Both timers (in-memory eviction + session-end timeout) reset on every user activity. Stored in `app/core/config/dynamic.py` (Redis-backed runtime config) so product can tune without redeploys. Defaults: `CHAT_IDLE_EVICTION_SECONDS=600` (10 min), `CHAT_SESSION_END_TIMEOUT_SECONDS=3600` (60 min). |
| Q4 | Outcome enum | **Reuse the voice outcome enum.** No chat-specific values for v1. Additive expansion later if product wants chat-only outcomes. |
| Q5 | LLM streaming compatibility | **All currently-wired providers stream natively.** Verified in pipecat 1.1: `pipecat/services/{azure,anthropic,google}/llm.py` all extend `pipecat/services/llm_service.py` and yield `LLMTextFrame` chunks during the LLM response. Streaming is the default — no extra config. Block mode is a server-side aggregation (collect all chunks, emit one `assistant_message` SSE event). |
| Q6 | Lock implementation | **Extract a small reusable helper.** Existing pattern is inlined in `app/core/background_tasks/scheduler.py:121-138` (Redis `SET NX EX`, set-and-forget). Chat needs real acquire/release/extend semantics, so extract `RedisLock` into `app/services/redis/locks.py`. Use compare-and-del Lua script for safe release. See §11.1. |
| Q7 | Webhook payload shape | **Mirror voice payload, add `channel: "web"`.** Canonical end-of-call shape from `callbacks/service_callback.py`: `{callSid, outcome, attemptCount, transcription (json string), callDuration, orderId, ...extracted_fields}`. Chat uses identical field names with `callSid = chat_session.id`, `attemptCount = 1`, `callDuration = ended_at - created_at`. Adding `channel` is non-breaking for tolerant consumers. Strict-schema validators may reject; document in merchant integration guide that strict consumers should configure a separate webhook URL. |

---

**Sign-off:** doc approved 2026-05-04. Implementation proceeds in the order listed in §16; Phase 1 ends at the §15 exit criteria.

**2026-05-04 rewrite:** chat mode migrated from Pipecat-pipeline-based execution to a direct LLM driver. Voice path untouched. See §4, §7, §9, and §16 for the post-rewrite shape; §3 D3 retains the original decision row marked superseded for historical context.
