# Clairvoyance

Conversational AI platform for real-time voice and chat interactions, built around **Breeze Buddy** — a template-driven agent for outbound/inbound telephony, web chat, and widget voice. Built on FastAPI + Pipecat-AI + asyncpg.

> **Note:** The former **Automatic** analytics voice agent (web/mobile via Daily.co) has been **removed**. Its functionality is migrated to an Automatic template-driven Breeze Buddy workflow (analytics tools exposed via global HTTP functions / MCP on a template). Open follow-ups tracked in `docs/AUTOMATIC_PARITY.md` (chart rendering, push-to-talk).

## Commands

```bash
# Setup
./scripts/setup.sh                  # Python 3.11 check, git hooks, uv install
uv sync --extra dev                 # Install with dev tools (black, isort, autoflake, pyrefly)

# Run
uv run python run.py                # Start FastAPI server on 0.0.0.0:8000

# Format (pre-commit hook runs these automatically)
uv run black .                      # Code formatting (line-length=88)
uv run isort . --profile black      # Import sorting
uv run autoflake --in-place --remove-all-unused-imports --remove-unused-variables --exclude "app/__init__.py,.venv/*,venv/*" -r app/

# Type check
uv run pyrefly check                # Type checking

# Dependencies
uv add <package>                    # Add production dependency
uv add --dev <package>              # Add dev dependency
uv lock --upgrade && uv sync        # Upgrade all
```

## Architecture

```
app/
├── main.py                         # FastAPI entry with lifespan (startup, shutdown)
├── ai/voice/
│   ├── agents/
│   │   └── breeze_buddy/           # Telephony + chat + widget agent (Twilio/Plivo/Exotel, Daily)
│   │       ├── agent/              # Core: pipeline.py, flow.py, transport.py, vad.py
│   │       ├── template/           # Template types, rendering, node transitions
│   │       ├── handlers/           # internal/ (builtin, warm_transfer, end_conversation)
│   │       │                       # transport/ (http_handler for global functions)
│   │       ├── services/           # telephony/{twilio,plivo,exotel}, daily/, agent_router/
│   │       ├── managers/           # CallsManager, agent lifecycle
│   │       ├── processors/         # Data transformation processors
│   │       ├── observability/      # OTEL tracing setup
│   │       └── tts/, stt/, llm/    # Provider configuration
│   ├── llm/                        # LLM provider wrappers
│   ├── stt/                        # STT providers: Google, Deepgram, Soniox, AssemblyAI, OpenAI
│   └── tts/                        # TTS providers: ElevenLabs, Cartesia, Google, Sarvam
├── api/routers/
│   ├── breeze_buddy/               # 22 files: leads, templates, analytics, websocket, auth
│   ├── devcycle.py                 # Feature flag webhooks
│   └── systems.py                  # Health checks, metrics
├── core/
│   ├── config/static.py            # Env var config (~198 vars, loaded once at startup)
│   ├── config/dynamic.py           # Redis-backed runtime config (DevCycle feature flags)
│   ├── logger/                     # Loguru: colored dev output, JSON structured prod output
│   ├── security/                   # JWT validation, password hashing, RBAC
│   ├── background_tasks/           # Scheduler with Redis distributed locking
│   └── transport/http_client.py    # aiohttp session factory with proxy support
├── database/
│   ├── migrations/                 # Sequential SQL: 001_initial_tables.sql, 002_...sql
│   ├── queries/                    # Raw parameterized SQL builders (return tuple[str, list])
│   ├── accessor/                   # Business logic layer (calls queries + decoders)
│   └── decoder/                    # DB rows -> Pydantic models
├── schemas/                        # Pydantic models (breeze_buddy/)
├── services/                       # External: redis/, aws/ (KMS, S3), gcp/, slack/, langfuse/
└── utils/                          # Common validation, parsing utilities
```

## Code Conventions

- **Python 3.11+**, managed with `uv` (not pip/poetry)
- **Black** formatting (line-length=88), **isort** (profile=black), **autoflake** (unused imports), **pyrefly** type checking
- **Naming**: snake_case functions/vars, PascalCase classes, SCREAMING_SNAKE_CASE constants (in static.py)
- **Imports**: stdlib -> third-party -> app (isort enforced)
- **Type hints**: Required on function signatures. Use `Optional[T]`, `List[T]`, `Dict[str, Any]`, `Union`
- **Pydantic models** for all API request/response schemas and data transfer
- **Async everything**: All DB, HTTP, and I/O operations are async/await
- **No ORM**: Raw asyncpg with parameterized queries (`$1, $2` placeholders). Three-layer pattern: queries (SQL builders) -> accessor (business logic) -> decoder (row to Pydantic)

## Git Workflow

- **Commit format**: `feat:`, `fix:`, `fix(scope):`, `refactor:`, `docs:` prefixes
- **IMPORTANT: PRs must contain exactly 1 commit** (enforced in CI)
- **Pre-commit hook** (`.githooks/pre-commit`): runs autoflake, isort, black, pyrefly check -- auto-formats and stages
- **CI checks** (`pr-build-check.yml`): black --check, isort --check, autoflake --check, pyrefly check, commit count = 1
- **Main branch**: `release`. PRs target `release`
- Run `git config core.hooksPath .githooks` if hooks aren't active (setup.sh does this)

## Breeze Buddy Patterns

Breeze Buddy is the template-driven telephony agent. These patterns MUST be followed when working in `app/ai/voice/agents/breeze_buddy/`:

### Template System
- Templates are JSON stored in PostgreSQL: `{initial_node, nodes: [{node_name, task_messages, functions, hooks}]}`
- Variables use `{placeholder}` syntax, resolved from lead payload at runtime
- Node transitions are LLM-driven via function calls with optional async hooks
- Template types defined in `breeze_buddy/template/types.py` -- this is the source of truth for all template models
- Every template create/update/rollback appends a snapshot row to `template_version` in the same transaction (append-only; active version = MAX(version_number); secrets never snapshotted). See `docs/TEMPLATE_VERSIONING.md`

### Lead Processing Flow
1. Lead inserted via `/push/lead/v2` -> validated -> stored as BACKLOG
2. Cron job picks up backlog leads
3. Pre-checks run (optional external API validation)
4. Call initiated via telephony provider (Twilio/Plivo/Exotel)
5. Agent loads template -> renders variables -> builds Pipecat pipeline
6. Conversation runs -> callbacks execute -> DB updated with outcome

### Handler Architecture
- **Internal handlers** (`handlers/internal/`): builtin_dispatcher, warm_transfer, end_conversation, STT, audio, outcome update
- **Transport handlers** (`handlers/transport/`): HTTP requests/responses for external integrations
- **Hook system**: Async side-effect functions (e.g., `update_outcome_in_database`, `set_transfer_flag`)
- **Global functions**: HTTP-based functions that block the flow and return data to LLM

### Configuration Hierarchy
1. **Static config** (`core/config/static.py`): Env vars, loaded once at startup, never re-read
2. **Dynamic config** (`core/config/dynamic.py`): Redis/DevCycle feature flags, async functions
3. **Template-level config**: STT/TTS provider, turn detection, interruption, VAD, warm transfer number
4. **Playground override**: Runtime config override via `is_playground=true` in lead payload

### Voice Pipeline
- **STT providers**: Soniox (default, native endpoint detection), Deepgram (SmartTurn), Sarvam, OpenAI, Google
- **TTS providers**: ElevenLabs (default), Cartesia, Sarvam -- template-level voice configuration
- **Turn detection modes**: `stt_native`, `smart_turn` (Whisper ONNX), `timeout`
- **VAD**: Silero with configurable confidence, start_secs, stop_secs, min_volume
- **Transport**: Daily.co for web, Twilio/Plivo/Exotel for telephony

### Error Handling
- `track_error(errors_list, error_message)` to collect errors
- `end_call_with_errors(ws, stream_sid, errors)` for graceful disconnect
- Fail-open graceful degradation (greeting prep, pre-checks with `default_on_failure`)
- `send_webhook_with_retry()` for reporting webhooks

### Observability
- **OTEL tracing** to Langfuse: root span with conversation_id (`customer_name-shop_name-timestamp`)
- **Log context** via `set_log_context()` / `update_log_context()` using contextvars (call_sid, lead_id, reseller_id, merchant_id)
- **Transcriptions**: Collected from LLM message history in `end_conversation`, stored as `{role, content}` array in `lead.metaData`

### Chat (text) mode
- Same template + FlowManager + LLM as voice; swaps audio I/O for text frames. Spec lives in `docs/CHAT_MODE.md`
- Code under `app/ai/voice/agents/breeze_buddy/chat/` (agent, transport, sse, cleanup) + router at `app/api/routers/breeze_buddy/chat.py`
- Templates opt in via `supported_channels: ["voice", "chat"]`; chat agents construct the builder with `disabled_names=CHAT_DISABLED_NAMES` (defined in `chat/disabled.py`) to strip voice-only functions/actions (mute_stt, play_audio_sound, warm_transfer, end_conversation, ...)
- **Stateless per turn**: `POST /message` builds a fresh `ChatAgent`, replays history from DB into `LLMContext`, drives one turn via `run_turn`, tears down. No in-memory registry, no sticky LB.
- Per-session `RedisLock` (180s TTL, no auto-extend) wraps the entire turn — single mutual-exclusion primitive across pods.
- Idle session cleanup is one task on the global `BackgroundTaskScheduler` (`chat/cleanup.py`); the scheduler's distributed lock keeps it single-pod-per-tick.
- Open follow-ups: LLM-generated greeting (today only `static_greeting` works), outcome webhook on `end()`

## Important

- IMPORTANT: Never modify migration SQL files directly. Create new sequential migrations (e.g., `026_your_change.sql`)
- IMPORTANT: The pre-commit hook auto-formats code. If CI fails on formatting, run `uv run black . && uv run isort . --profile black` locally
- Secrets and credentials are KMS-encrypted in the database. Use `SKIP_KMS_DECRYPT=true` locally if no AWS access
- `.env.example` -- copy to `.env` and fill required ones for your work area (some entries are legacy Automatic vars, now unused)
- Redis distributed locking prevents duplicate background task execution across pods
- CORS is configured in main.py via `CORS_ALLOWED_ORIGINS` env var
