# Clairvoyance

Multi-agent conversational AI platform for real-time voice interactions. Two main agents: **Automatic** (analytics voice agent for web/mobile) and **Breeze Buddy** (template-driven telephony voice agent for outbound/inbound calls). Built on FastAPI + Pipecat-AI + asyncpg.

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
uv run pyrefly check                # Type checking (excludes **/automatic/**)

# Dependencies
uv add <package>                    # Add production dependency
uv add --dev <package>              # Add dev dependency
uv lock --upgrade && uv sync        # Upgrade all
```

## Architecture

```
app/
├── main.py                         # FastAPI entry with lifespan (pool init, shutdown)
├── ai/voice/
│   ├── agents/
│   │   ├── automatic/              # Analytics voice agent (web/mobile via Daily.co)
│   │   │   ├── tools/              # Dynamic tools: Juspay, Breeze, Charts, System, Internet
│   │   │   ├── features/           # HITL, charts, text sanitization
│   │   │   └── rtvi/               # RTVI protocol integration
│   │   └── breeze_buddy/           # Telephony voice agent (Twilio/Plivo/Exotel)
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
│   ├── automatic.py                # Automatic agent endpoints
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
├── schemas/                        # Pydantic models (breeze_buddy/, automatic_voice/)
├── services/                       # External: redis/, aws/ (KMS, S3), gcp/, slack/, langfuse/
├── helpers/automatic/              # daily_room_pool.py, process_pool.py, session_manager.py
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

## Important

- IMPORTANT: Never modify migration SQL files directly. Create new sequential migrations (e.g., `026_your_change.sql`)
- IMPORTANT: The pre-commit hook auto-formats code. If CI fails on formatting, run `uv run black . && uv run isort . --profile black` locally
- IMPORTANT: `pyrefly check` excludes `**/automatic/**` (see pyproject.toml). Only Breeze Buddy code is type-checked
- Secrets and credentials are KMS-encrypted in the database. Use `SKIP_KMS_DECRYPT=true` locally if no AWS access
- `.env.example` has 198 variables -- copy to `.env` and fill required ones for your work area
- Process pools (Daily rooms + voice agents) are pre-warmed at startup to avoid 5-6s latency. Don't bypass this
- Redis distributed locking prevents duplicate background task execution across pods
- CORS is configured in main.py via `CORS_ALLOWED_ORIGINS` env var
- Voice agent subprocesses are spawned via `process_pool.py` -- each call runs in its own process
