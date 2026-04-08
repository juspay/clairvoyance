# Breeze Buddy Agent

Template-driven telephony voice agent for automated outbound/inbound calls. Supports Twilio, Exotel, and Plivo with JSON-defined conversation flows.

## Feature Documentation

| Feature | Description |
|---------|-------------|
| [Architecture](architecture/) | Overall system architecture, template engine, data flow |
| [Voice Configuration](voice_configuration/) | TTS provider configuration (Cartesia, ElevenLabs) |
| [Warm Transfer](warm_transfer/) | Call transfer to human agents (Twilio, Exotel, Plivo) |
| [Interruption Control](interruption_control/) | User interruption handling modes and node-level switching |
| [Input Collection](input_collection/) | Multi-segment input accumulation (phone numbers, addresses) |
| [Keyword Filter](keyword_filter/) | Transcription filtering to prevent false interruptions |
| [Pre-Checks](pre_checks/) | External API validation before initiating calls |
| [Merchant & User Management](merchant_user_management/) | RBAC, merchant entities, user accounts |
| [Demo](demo/) | Public demo endpoint for frontend integration |

## Example Templates

Template configuration examples are in `app/ai/voice/agents/breeze_buddy/examples/templates/`.

## Key Code Paths

- **Agent**: `app/ai/voice/agents/breeze_buddy/`
- **API**: `app/api/routers/breeze_buddy/`
- **Database**: `app/database/accessor/breeze_buddy/`
- **Schemas**: `app/schemas/breeze_buddy/`
