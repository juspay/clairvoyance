---
paths:
  - "app/ai/voice/agents/breeze_buddy/**/*.py"
  - "app/api/routers/breeze_buddy/**/*.py"
  - "app/database/accessor/breeze_buddy/**/*.py"
  - "app/database/queries/breeze_buddy/**/*.py"
  - "app/schemas/breeze_buddy/**/*.py"
---

# Breeze Buddy Rules

## Template System
- Template types are defined in `app/ai/voice/agents/breeze_buddy/template/types.py` -- always check this file before modifying template logic
- Templates use `{placeholder}` variable syntax resolved from lead payload -- never hardcode values that should come from payload
- Node transitions are LLM-driven via function calls. Each function can have async hooks for side effects
- When adding new template features, update both `types.py` models AND the rendering logic in `agent/flow.py`

## Handler Pattern
- Internal handlers go in `handlers/internal/` -- these handle audio, STT events, warm transfer, conversation end
- Transport handlers go in `handlers/transport/` -- these handle HTTP requests to external services
- New handlers must follow the existing signature pattern: accept agent context and return appropriate response
- Hooks are async side-effect functions -- they should NOT block the main conversation flow unless explicitly designed as global functions

## Database Access
- Follow the three-layer pattern: `queries/breeze_buddy/` (SQL) -> `accessor/breeze_buddy/` (logic) -> `decoder/breeze_buddy/` (row to Pydantic)
- All queries use asyncpg parameterized format (`$1, $2`). Never use string interpolation in SQL
- Accessor functions handle error logging and re-raise. Decoders are pure transformation functions

## Telephony
- Each provider (Twilio, Plivo, Exotel) has its own service directory under `services/telephony/`
- Provider-specific WebSocket handling, recording downloads, and callback formats differ significantly
- Warm transfer flow: LLM calls transfer function -> handler sets Redis flag -> provider-specific bridging
- Always test with the specific provider, not just Daily.co web transport

## Observability
- Set log context at call start via `set_log_context(call_sid=..., lead_id=..., reseller_id=..., merchant_id=...)`
- OTEL root span uses conversation_id format: `customer_name-shop_name-timestamp`
- Transcriptions are collected from LLM message history in end_conversation handler
