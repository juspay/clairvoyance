# Clairvoyance AI Coding Agent Guide

## Project Overview
Clairvoyance is a multi-agent conversational AI platform for real-time voice interactions built on FastAPI and Pipecat. It orchestrates two specialized voice agents: **Automatic** (analytics/data conversations) and **Breeze Buddy** (telephony/workflow-driven calls).

## Architecture Fundamentals

### Multi-Agent Design
- **Automatic Agent**: Pipecat-based, supports `live` (real data) and `test` modes, uses MCP for remote tooling
- **Breeze Buddy Agent**: Template-driven telephony agent with JSON-defined conversation flows, integrates with Twilio/Exotel/Plivo
- Agents run as **subprocesses** launched by the main FastAPI server, not as endpoints

### Dual Pool Optimization (Critical)
Connection time reduced from ~8s to 3-4s via two pre-warmed pools:
- **VoiceAgentPool** (`app/helpers/automatic/process_pool.py`): Pre-initialized agent processes (5-6s startup avoided)
- **DailyRoomPool** (`app/helpers/automatic/daily_room_pool.py`): Pre-created Daily.co rooms with tokens (1s room creation avoided)
- Background workers automatically replenish pools when low
- Configured via `VOICE_AGENT_POOL_SIZE`, `DAILY_ROOM_POOL_SIZE` env vars

### Database-First Configuration
- PostgreSQL stores templates, call configs, sessions (`app/database/migrations/`)
- Templates define conversation flows as JSON with node-based navigation (`docs/BREEZE_BUDDY_ARCHITECTURE.md`)
- Use `python -m scripts.create_tables create` to initialize schema

### Redis for State & Caching
- Feature flags cached in Redis, refreshed from DevCycle (`app/services/redis/`)
- Auto-refresh worker pattern: register keys with callbacks to fetch fresh data before expiry
- Namespace isolation: `feature_flags`, `sessions`, `users`, etc.

## Development Workflows

### Running Locally
```bash
# Load .env FIRST (run.py enforces this order)
python run.py  # Uvicorn with auto-reload enabled

# Initialize DB tables
python -m scripts.create_tables create

# Setup script handles additional config
./scripts/setup.sh
```

### Environment Variables (Static Config Pattern)
- **ALL** config loaded from `app/core/config/static.py` using `get_required_env()` for mandatory vars
- Never import from `os.environ` directly elsewhere
- Examples: `DAILY_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `DAILY_ROOM_POOL_SIZE`

### Adding New Agents
1. Create agent module under `app/ai/voice/agents/{agent_name}/`
2. Implement as subprocess accepting CLI args (see `app/ai/voice/agents/automatic/__init__.py`)
3. Add router in `app/api/routers/`
4. Register in `app/main.py` lifespan manager

## Code Patterns

### Pipecat Pipeline Structure
Agents use STT → LLM → TTS pipeline. Key services:
- STT: Google/AssemblyAI/Deepgram
- LLM: Azure OpenAI with function calling
- TTS: ElevenLabs/Sarvam/Cartesia (configurable via env)

### Template-Driven Conversations (Breeze Buddy)
- Templates stored in `templates` table with `expected_payload_schema` (JSON Schema validation)
- Payload variables injected into prompts via `{{variable}}` syntax
- Node transitions triggered by LLM function calls
- Hooks execute async side-effects (DB updates, external APIs)

### Background Task Scheduler
- `BackgroundTaskScheduler` (`app/core/background_tasks/scheduler.py`) with distributed Redis locking
- Prevents duplicate execution across multiple pods
- Register tasks: `scheduler.register_task(name="task_name", func=async_func, interval_seconds=300)`

### Remote Tooling via MCP
- Automatic agent fetches tools from MCP server over SSE (`app/ai/voice/agents/automatic/services/mcp/`)
- Pydantic validation ensures spec compliance
- Tools dynamically registered with LLM without redeployment

### Security Patterns
- JWT validation for Automatic agent requests (`app/core/security/jwt.py`)
- RBAC tokens for Breeze Buddy (`app/api/security/breeze_buddy/rbac_token.py`)
- SHA-based signing for MCP requests (`app/core/security/sha.py`)

## Common Pitfalls

### Import Order in run.py
`dotenv.load_dotenv()` **must** be called before any app imports. Violating this breaks config loading.

### Process Pool vs Direct Launch
Never bypass pools for new connections—fallback only if exhausted. Pools maintain health checks and auto-replenishment.

### Database Migrations
Apply SQL migrations manually in sequence (`app/database/migrations/`). No ORM—raw SQL via `asyncpg`.

### Redis Namespace Collisions
Always use `namespace` parameter in `redis_get`/`redis_set` to avoid key conflicts across services.

## Testing & Debugging

### Daily.co Room Management
- Monitor pool status: `/pool/rooms/status` endpoint
- Rooms are single-use, cleaned up after calls
- Token expiry auto-validated, refreshed in background

### Langfuse Tracing
- OpenTelemetry integration for observability (`app/services/langfuse/`)
- Auto-evaluation tasks registered in `BackgroundTaskScheduler`
- Scores stored in `langfuse_scores` column (migration 010)

### Agent Connection Flow
See `docs/AGENT_CONNECTION_FLOW.md` for WebSocket handshake and subprocess lifecycle details.

## Key Files Reference
- **Entry point**: `run.py` (loads .env, initializes DevCycle, starts Uvicorn)
- **Main app**: `app/main.py` (lifespan manager, pool initialization, routers)
- **Pool implementations**: `app/helpers/automatic/{process_pool,daily_room_pool}.py`
- **Redis service**: `app/services/redis/client.py` (get/set/refresh patterns)
- **Background tasks**: `app/core/background_tasks/scheduler.py`
- **Breeze Buddy templates**: `docs/BREEZE_BUDDY_ARCHITECTURE.md` (comprehensive guide)

## Docker Deployment
- Multi-stage build with audio processing deps (`Dockerfile`)
- Krisp audio filter model downloaded from GCS using build secrets
- Health checks via `/health` endpoint
