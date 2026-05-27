# Daily Warm Transfer (Telephony Bridge)

Design doc for warm-transferring a Daily (web/mobile) call to a human agent reachable only by phone (PSTN).

## Status

Implemented — V1 (Plivo only). Exotel can be added by wiring its serializer in
`transfer_bridge._build_telephony_serializer`; Twilio requires a separate dial
path (its `make_call` does not route through `/answer`).

## Problem

Today, `connect_to_live_agent` ([handlers/internal/warm_transfer.py](../../app/ai/voice/agents/breeze_buddy/handlers/internal/warm_transfer.py)) only works when the original customer call is itself a telephony call (Twilio / Plivo / Exotel). It uses each provider's conference / native-transfer API to bridge the customer's existing phone leg with a freshly-dialed agent leg.

For Daily-mode leads (`ExecutionMode.DAILY*`), the customer is in a WebRTC room — there is no phone leg to bridge into. The function silently has no path for them.

## Goal

When the LLM in a Daily call invokes `connect_to_live_agent`:

1. Dial the configured human agent on PSTN via the lead's existing telephony provider.
2. Bridge that phone leg into the Daily room so customer (in Daily) and agent (on phone) talk live.
3. AI bot leaves the room as soon as the bridge is live. No whisper / coaching.

## Why a bridge bot, not Daily PSTN dial-out

Daily.co supports native PSTN dial-out (`dialOut` API), which would add the agent's phone as a Daily participant directly with no bridge. We are **not** taking that path because:

- Reuses existing Twilio / Plivo / Exotel outbound infrastructure, billing, outbound-number selection per reseller.
- Keeps `lead.metaData.transfer` payload shape identical to the telephony warm-transfer flow → analytics queries don't fork.
- No dependency on Daily PSTN credits / SIP trunk provisioning.

The trade-off: we own the audio resampling and one extra Pipecat task per transfer.

## Why in-process, not subprocess

Originally planned as a subprocess (mirroring the AI bot's process model). Pivoted to **in-process**: the provider's WebSocket lands in the FastAPI server, so handling it in a new WS endpoint is direct. A subprocess would require shuttling audio frames over IPC (Redis pub-sub or unix socket) — adds latency, complexity, and a new failure surface for marginal isolation benefit. Crash isolation is per-handler: an exception in one bridge only kills that one transfer.

## Architecture

The bridge runs inside the FastAPI server as a WebSocket handler. When the dialed agent picks up, the telephony provider opens a WS to `/{provider}/bridge/v2` (e.g. `/agent/voice/breeze-buddy/plivo/bridge/v2`). That handler reads the Redis bridge flag, joins the existing Daily room as a Pipecat client, and runs a forwarding pipeline: telephony WS frames → resample/encode → Daily; Daily frames → resample/encode → telephony WS. The call identity (`call_id` / `stream_id`) is parsed from the provider's WebSocket handshake frames (via `parse_telephony_websocket`), not query parameters.

## Sequence

| Step | Actor              | Action                                                                                                         |
| ---- | ------------------ | -------------------------------------------------------------------------------------------------------------- |
| 1    | LLM (in AI Bot)    | Calls `connect_to_live_agent` function                                                                         |
| 2    | AI Bot             | Resolves `outbound_number` from lead, picks provider                                                           |
| 3    | AI Bot             | `set_bridge_flag(agent_call_sid_placeholder, room_url, room_name, lead_id, provider)` in Redis                 |
| 4    | AI Bot             | `provider.make_call(agent_phone, outbound_number)` → returns `agent_call_sid`                                  |
| 5    | AI Bot             | Updates Redis flag key from placeholder to real `agent_call_sid`                                               |
| 6    | AI Bot             | Waits (with 30s timeout) for `bridge:{agent_call_sid}.status == "joined"`                                      |
| 7    | Provider           | Phone rings; agent picks up; provider POSTs answer webhook                                                     |
| 8    | Webhook dispatcher | Reads `bridge:{call_sid}` flag → returns `<Connect><Stream url=…/daily/transfer/bridge-ws?call_sid=…>`         |
| 9    | Bridge WS handler  | Provider opens WS; handler joins Daily room R with bot token, builds forwarding pipeline                       |
| 10   | Bridge WS handler  | Sets `bridge:{call_sid}.status = "joined"`                                                                     |
| 11   | AI Bot             | Sees joined status → `end_conversation`, leaves Daily room                                                     |
| 12   | Bridge WS handler  | Pumps audio in both directions until either leg ends                                                           |
| 13   | Bridge WS handler  | On either leg ending: hangup the other; `update_lead_call_completion_details` + write `lead.metaData.transfer` |

## Audio Bridging

| Direction        | Source             | Resample                                 | Encode                                   | Sink               |
| ---------------- | ------------------ | ---------------------------------------- | ---------------------------------------- | ------------------ |
| Customer → Agent | Daily 24kHz PCM    | `audioop.ratecv 24000 → 8000` (stateful) | `lin2ulaw`                               | Telephony WS μ-law |
| Agent → Customer | Telephony WS μ-law | `ulaw2lin`                               | `audioop.ratecv 8000 → 24000` (stateful) | Daily 24kHz PCM    |

**State-keeping is critical**: `audioop.ratecv` returns a state tuple that must be passed back into the next call to avoid resampling clicks/discontinuities. Two `FrameProcessor` subclasses, one per direction, each holding their own `state` attribute.

Primitives already exist in [utils/common.py:70-112](../../app/ai/voice/agents/breeze_buddy/utils/common.py#L70-L112) (used today for non-realtime greeting audio); we wrap them in stateful realtime processors.

## Redis Bridge Flag

Key: `bridge:{call_sid}` (the _agent leg's_ call_sid, set after `make_call` returns)

```json
{
  "room_url": "https://breezebuddy.daily.co/abc123",
  "room_name": "abc123",
  "lead_id": "lead_xyz",
  "provider": "twilio",
  "outbound_number": "+1...",
  "agent_phone": "+91...",
  "status": "dialing | joined | failed",
  "failure_reason": "no_answer | busy | timeout | crash | null",
  "created_at": 1736300000.0
}
```

TTL: 2h (matches existing `transfer:{call_sid}` flag).

## Provider differences

The dispatcher-detect approach (preferred — see plan) means **no provider API changes**: each provider's existing inbound webhook entrypoint already handles `<Connect><Stream>` for AI calls; we add a flag check at the top to switch to the bridge stream URL when present.

| Provider | Webhook entry                         | Notes                                   |
| -------- | ------------------------------------- | --------------------------------------- |
| Twilio   | `POST /v2/twilio/callback/{template}` | Returns TwiML `<Connect><Stream>`       |
| Plivo    | `POST /v2/plivo/answer/{template}`    | Returns Plivo XML `<Stream>`            |
| Exotel   | Applet-driven webhook                 | Stream URL returned via applet response |

The bridge WebSocket endpoint must speak each provider's wire format (Twilio binary frame format vs Plivo vs Exotel) — Pipecat's telephony transports already abstract this. The bridge handler picks the transport based on the `{provider}` path parameter.

## Failure Modes

| Scenario                                         | Detection                                                      | Recovery                                                                                                                       |
| ------------------------------------------------ | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Agent doesn't answer (no_answer / busy / failed) | Provider status callback fires before `bridge.status = joined` | Set `bridge.status = failed`, `failure_reason`. AI bot poll/sub sees this → tells LLM transfer failed → conversation continues |
| Answer timeout (30s)                             | AI bot wait expires                                            | Provider hangup API on `agent_call_sid`; flag → failed; LLM informed                                                           |
| Customer leaves Daily mid-dial                   | Daily participant-left event in bridge handler                 | AI bot cancels outbound via provider hangup; bridge exits cleanly                                                              |
| Bridge handler exception pre-join                | `bridge.status` never flips to joined; AI bot timeout          | Same as answer timeout                                                                                                         |
| Telephony WS drops mid-call                      | WS disconnect event in handler                                 | Tear down Daily side; do **not** reconnect (PSTN won't reconnect cleanly); write transfer status `disconnected`                |
| Agent hangs up normally                          | Telephony provider status callback `completed`                 | Bridge handler leaves Daily; lead marked complete                                                                              |

## What gets shipped

New code:

- `app/ai/voice/agents/breeze_buddy/utils/bridge_flag.py` — Redis helpers for `bridge:{call_sid}`.
- `app/ai/voice/agents/breeze_buddy/handlers/internal/daily_warm_transfer.py` — handler invoked by `connect_to_live_agent` for Daily mode.
- `app/ai/voice/agents/breeze_buddy/services/daily/transfer_bridge.py` — async pipeline builder + run loop. Pure function called from the WS handler.
- `app/api/routers/breeze_buddy/telephony/bridge.py` — new WebSocket endpoint `/{provider}/bridge/ws` that the dialed agent leg connects to.

Modified:

- [app/ai/voice/agents/breeze_buddy/handlers/internal/warm_transfer.py](../../app/ai/voice/agents/breeze_buddy/handlers/internal/warm_transfer.py) — branch on `execution_mode` at the top of `connect_to_live_agent`.
- [app/api/routers/breeze_buddy/telephony/answer/handlers.py](../../app/api/routers/breeze_buddy/telephony/answer/handlers.py) — bridge-flag check at the top of `handle_provider_answer`; if set, return WS URL pointing at the bridge endpoint.
- [app/api/routers/breeze_buddy/telephony/**init**.py](../../app/api/routers/breeze_buddy/telephony/__init__.py) — register bridge router.

## Out of Scope

- Whisper / coaching audio to agent before bridging in customer.
- AI bot staying in room as silent transcription observer.
- Per-leg recordings (Daily room cloud recording captures the full bridged audio).
- Native Daily PSTN dial-out — alternative architecture, deferred.
