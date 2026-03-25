# Automatic Agent

Pipecat-based voice agent for dynamic data retrieval and analytics conversations. Supports live mode (real data) and test mode (demo data) with dynamic tool loading via MCP.

## Feature Documentation

| Feature | Description |
|---------|-------------|
| [Connection Flow](connection_flow/) | Client connection lifecycle, WebRTC transport, pipeline architecture |
| [Pool](pool/) | Dual pool optimization (Daily rooms + voice agent processes) |

## Key Code Paths

- **Agent**: `app/ai/voice/agents/automatic/`
- **Pool Management**: `app/helpers/automatic/`
- **API**: `app/api/routers/automatic/`
- **Tools**: `app/ai/voice/agents/automatic/tools/`
