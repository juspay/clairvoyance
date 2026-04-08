# Automatic Voice Agent - Connection Flow

## Overview

The Automatic Voice Agent is a real-time conversational AI system built on the [Pipecat](https://github.com/pipecat-ai/pipecat) framework. It uses Daily.co WebRTC for audio transport, configurable STT/TTS providers for speech processing, and Azure OpenAI as the LLM. Sessions are managed through pre-warmed process and room pools to minimize connection latency.

## Connection Flow

### 1. Client Connects

A client sends a `POST` request to `/agent/voice/automatic` with an `AutomaticVoiceUserConnectRequest` payload containing session parameters: mode (live/test), user identity, shop credentials, TTS preferences, and integration tokens.

The endpoint handler in `app/main.py` (`bot_connect`) performs the following steps:

1. **Consolidates request parameters** into a `session_params` dictionary.
2. **Generates a session ID** (`uuid4`) and retrieves a **pre-created Daily room** from the `DailyRoomPool`.
3. **Attempts to acquire a pre-warmed process** from the `VoiceAgentPool`. If successful, it writes the session configuration as JSON to the process's `stdin`.
4. **Falls back to launching a new subprocess** directly (`sys.executable -m app.ai.voice.agents.automatic`) if the pool is exhausted or the pool write fails.
5. **Registers the process** in the global `bot_procs` tracking dictionary (keyed by PID).
6. **Returns** `{ room_url, token, session_id }` to the client. The client uses the `token` and `room_url` to join the Daily room via WebRTC.

### 2. Daily Room Pool (`app/helpers/automatic/daily_room_pool.py`)

The `DailyRoomPool` eliminates per-request room creation latency:

- At startup, it pre-creates a configurable number of Daily rooms (`DAILY_ROOM_POOL_SIZE`, default 1), each with a user token (non-owner) and a bot token (owner), both with long expiry (7 days).
- On `get_room(session_id)`, it dequeues a room, validates expiry, refreshes tokens if needed, and marks it active. If the pool is low, it schedules background replenishment.
- If the pool is empty, it falls back to creating a room on-demand with a shorter session-scoped expiry.
- On session end, `cleanup_and_replenish_room` deletes the used room and triggers background creation of a replacement.

### 3. Voice Agent Process Pool (`app/helpers/automatic/process_pool.py`)

The `VoiceAgentPool` eliminates the 5-6 second cold-start of loading AI models:

- Pre-warmed processes are launched with `--pool-mode --process-id <id>`. They run `pre_initialize_services()` (pre-loads the Silero VAD model), then print `READY` to stdout and block on `stdin` waiting for session assignments.
- When assigned a session, the pool writes a JSON config line to `stdin`. The process deserializes it in `handle_session()`, runs the full `run_normal_mode()` pipeline, and prints `SESSION_ENDED` when done, making it available for the next session.
- Unhealthy processes are detected and replaced. The pool auto-replenishes as processes are consumed.

## WebRTC Transport Setup

The voice agent creates a `DailyTransport` configured with:

- **Audio in/out enabled** for bidirectional voice.
- **Silero VAD** (Voice Activity Detection) for turn-taking, with configurable confidence, start/stop thresholds, and minimum volume; can be disabled via config.
- **AIC audio filter** (optional) for noise enhancement on the input audio stream.
- The bot joins the Daily room using the bot token (owner privileges) with the display name "Breeze Automatic Voice Agent".

## Pipeline Architecture

The Pipecat pipeline is assembled as an ordered list of frame processors:

```
DailyTransport.input()
  -> [PTTVADFilter]           (optional, for push-to-talk mode)
  -> STT Service
  -> [STTMuteFilter]          (optional, mutes STT until first bot response completes)
  -> RTVIProcessor
  -> ContextAggregator.user()
  -> LLMServiceWrapper(AzureLLMService)
  -> LLMSpyProcessor          (intercepts tool calls, HITL confirmations, chart events)
  -> TTS Service
  -> DailyTransport.output()
  -> ContextAggregator.assistant()
```

### STT Providers (`app/ai/voice/agents/automatic/stt/`)

Selected via the `STT_PROVIDER` config. Options:

| Provider    | Notes |
|-------------|-------|
| **Deepgram** (default for most voices) | Configurable model, language, endpointing, smart format, VAD events |
| **Google**   | Default fallback if no provider is specified |
| **Sarvam**   | Used for the RHEA voice or when `STT_PROVIDER=sarvam`; supports Hindi and other Indic languages |
| **OpenAI**   | Used for the MIA voice (when override enabled) or when `STT_PROVIDER=openai` |
| **Soniox**   | When `STT_PROVIDER=soniox` |
| **AssemblyAI** | When `STT_PROVIDER=assemblyai` |

### LLM

**Azure OpenAI** (`AzureLLMService`) is the sole LLM provider, wrapped in `LLMServiceWrapper` which provides summarizing context management. The LLM uses a system prompt personalized with the user's name, TTS provider, and shop ID.

### TTS Providers (`app/ai/voice/agents/automatic/tts/`)

Selected via the `ttsService.ttsProvider` and `ttsService.voiceName` request parameters:

| Provider      | Voice     | Notes |
|---------------|-----------|-------|
| **Google TTS** | BRET (default), MIA | Primary provider |
| **Sarvam TTS** | RHEA | For Indic language support; configurable model, pitch, pace |
| **ElevenLabs** | RHEA | Alternative for RHEA voice |

## Dynamic Tool Loading (`app/ai/voice/agents/automatic/tools/__init__.py`)

Tools are loaded by `initialize_tools()` based on mode and available credentials:

**Always loaded:**
- **System tools** (e.g., `get_current_time`)
- **Internet/search tools** (when `ENABLE_SEARCH_GROUNDING` is on)
- **Chart tools** (when `ENABLE_CHARTS` is on)

**Test mode:**
- **Dummy tools** providing mock data, or **ACME analytics tools** for the `acme-store-demo` shop.

**Live mode (credential-gated):**
- **Juspay tools** -- loaded when an `euler_token` is provided. Credentials are set on the module before registration.
- **Breeze analytics tools** -- loaded when a `breeze_token`, `shop_id`, `shop_url`, and `shop_type` are all provided.
- **Breeze configuration tools** -- loaded when a `breeze_token`, `shop_id`, `shop_url`, and `merchant_id` are all provided.

**Write-action filtering:** If `AUTOMATIC_WRITE_ACTIONS_AUTHORIZED_USERS` is configured, tools are filtered by user email authorization before registration.

**MCP alternative:** When `ENABLE_BREEZE_MCP` is enabled (checked from Redis), tools are loaded from a remote Breeze MCP server instead of the local tool modules. The MCP context includes all session parameters (tokens, shop info, user info, mode).

## Session Lifecycle

### Connect

1. Client POSTs to `/agent/voice/automatic`.
2. Server assigns a Daily room and a voice agent process.
3. Server returns `room_url` + `token` to the client.
4. Client joins the Daily room using the WebRTC token.

### Conversation

1. `on_first_participant_joined` fires when the client enters the room.
   - Optionally starts Daily cloud recording (`ENABLE_AUTOMATIC_DAILY_RECORDING`).
   - Queues an `LLMRunFrame` to trigger the bot's opening message.
2. The pipeline processes audio bidirectionally: user speech is transcribed (STT), sent to the LLM with tool context, and the LLM response is synthesized (TTS) and streamed back.
3. Tool calls trigger spoken feedback ("Let me check on that...") via `on_function_calls_started` when using Google TTS, or play a tool-call sound effect if configured.
4. Idle timeout (`AUTOMATIC_SESSION_INACTIVITY_TIMEOUT`) auto-cancels the pipeline if no bot speech or LLM response occurs within the threshold.
5. Interruptions are enabled (`allow_interruptions=True`), so the user can interrupt the bot mid-speech.

### Cleanup

1. `on_participant_left` fires when the client leaves the room.
   - Stops recording if enabled.
   - Cancels the pipeline task.
2. `on_pipeline_finished` cancels the asyncio main task, causing the runner to exit.
3. In pool mode, the process prints `SESSION_ENDED` and becomes available for the next session.
4. In direct mode, the process exits.
5. The `SessionManager` background monitor (`monitor_session_cleanup`) periodically scans `bot_procs` for terminated processes and removes stale entries.
6. On application shutdown, `cleanup_bot_processes()` terminates all tracked processes (SIGTERM, then SIGKILL after timeout), and `cleanup_room_pool()` deletes all managed Daily rooms.

## Event Handlers

| Handler | Trigger | Action |
|---------|---------|--------|
| `on_first_participant_joined` | First user joins the Daily room | Starts recording (if enabled), queues `LLMRunFrame` to begin conversation |
| `on_participant_left` | User leaves the Daily room | Stops recording (if enabled), cancels pipeline task |
| `on_client_ready` | RTVI client signals readiness | Sends `bot-ready` server message via RTVI |
| `on_client_message` | RTVI client sends a message | Routes `function-confirmation-response`, `ptt-start`, `ptt-end`, and `ptt-sync` messages |
| `on_app_message` | Daily transport app message | Routes function confirmation and PTT messages to the RTVI handler |
| `on_pipeline_finished` | Pipeline task completes or is cancelled | Cancels the main asyncio task for graceful shutdown |

## HITL (Human-in-the-Loop) Function Confirmation

The system supports requiring explicit user approval before executing certain tool calls ("dangerous operations"):

1. **Detection:** The `LLMSpyProcessor` intercepts `FunctionCallInProgressFrame` frames. The `is_dangerous_operation()` utility determines whether a tool call requires confirmation.
2. **Request:** When confirmation is needed, the processor sends a `function-confirmation-request` message to the client via RTVI, containing a unique `confirmationId` and details about the function and its arguments.
3. **Pending state:** A `register_pending_confirmation(confirmationId)` call creates an `asyncio.Future` stored in a global dictionary, and the processor awaits `wait_for_confirmation_response()` with a configurable timeout.
4. **Client response:** The client sends a `function-confirmation-response` message (via RTVI or Daily app message) containing `confirmationId`, `approved` (boolean), and an optional `reason`.
5. **Resolution:** `handle_confirmation_response()` resolves the pending Future, unblocking the processor. If approved, the tool call proceeds. If denied or timed out, the tool call is skipped and the LLM is informed.

## Key Source Files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app, `/agent/voice/automatic` endpoint, lifespan management |
| `app/ai/voice/agents/automatic/__init__.py` | Voice agent entry point, pipeline assembly, event handlers |
| `app/helpers/automatic/daily_room_pool.py` | Pre-created Daily room pool management |
| `app/helpers/automatic/process_pool.py` | Pre-warmed voice agent subprocess pool |
| `app/helpers/automatic/session_manager.py` | Global process tracking, cleanup monitor, shutdown handler |
| `app/ai/voice/agents/automatic/tools/__init__.py` | Dynamic tool loading based on mode and credentials |
| `app/ai/voice/agents/automatic/stt/__init__.py` | STT provider selection |
| `app/ai/voice/agents/automatic/tts/__init__.py` | TTS provider selection |
| `app/ai/voice/agents/automatic/processors/llm_spy.py` | LLM frame interception, HITL confirmation, chart events |
| `app/api/routers/automatic.py` | Pool status and session cleanup API endpoints |
