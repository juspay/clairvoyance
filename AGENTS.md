# Clairvoyance

Multi-agent conversational AI platform for real-time voice interactions. Built on FastAPI + Pipecat-AI + PostgreSQL (asyncpg).

## HARD RULES (override everything else)

- **No cloud-provider access on the agent's own initiative.** Never SSH into, tunnel to, or call APIs/consoles of AWS, GCP, or any cloud resource (compute instances, buckets, clusters, cloud-hosted services), and never deploy/run anything there — only when the user explicitly asks for exactly that in the CURRENT conversation. Permissions from older conversations do not carry over.
- **No critical or irreversible actions on the agent's own.** Never delete/overwrite/drop data, restart/reconfigure the user's running processes, write to production/shared state, or publish externally without asking first. Ask, get explicit approval, then act.

## Project Structure

- `app/ai/voice/agents/` -- Voice agent implementations (Automatic for web analytics, Breeze Buddy for telephony)
- `app/api/routers/` -- FastAPI REST + WebSocket endpoints
- `app/core/config/` -- Static (env vars) and dynamic (Redis/DevCycle) configuration
- `app/database/` -- asyncpg with three-layer pattern: queries (SQL) -> accessor (logic) -> decoder (models)
- `app/schemas/` -- Pydantic request/response models
- `app/services/` -- External integrations: Redis, AWS KMS/S3, GCP Storage, Slack, Langfuse

## Commands

- `uv sync --extra dev` -- Install dependencies
- `uv run python run.py` -- Start server (0.0.0.0:8000)
- `uv run black . && uv run isort . --profile black` -- Format code
- `uv run pyrefly check` -- Type check (excludes **/automatic/**)

## Conventions

- Python 3.11+, managed with `uv`
- Black (line-length=88) + isort (profile=black) + autoflake formatting
- Async/await for all I/O. No ORM -- raw asyncpg with `$1, $2` parameterized queries
- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`
- PRs must contain exactly 1 commit
- Never modify existing SQL migrations -- create new sequential files

## Key Patterns

- **Breeze Buddy templates**: JSON-defined conversation flows in PostgreSQL, with `{variable}` substitution from lead payload
- **Voice pipeline**: Pipecat subprocesses per call. STT (Soniox/Deepgram/Sarvam), TTS (ElevenLabs/Cartesia/Sarvam), LLM (Azure OpenAI)
- **Configuration cascade**: env vars (static) -> Redis/DevCycle (dynamic) -> template-level -> playground override
- **Observability**: Loguru logging with contextvars, OTEL tracing to Langfuse, Slack alerts
- **Process pools**: Pre-warmed Daily rooms and voice agent subprocesses to avoid cold start latency
