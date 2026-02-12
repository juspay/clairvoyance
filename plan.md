# Plan: Add Plivo Inbound Call Flow Support

## Current State Analysis

### What Exists for Plivo (Outbound Only)
- `PlivoProvider` class with `make_call()` and `handle_websocket()` (`app/ai/voice/agents/breeze_buddy/services/telephony/plivo/plivo.py`)
- `handle_plivo_answer()` endpoint at `POST /plivo/answer` - returns XML `<Stream>` for outbound calls only (`app/api/routers/breeze_buddy/telephony/callbacks/handlers.py:198-255`)
- Recording support: `start_call_recording()` and `download_call_recording()` (`plivo/recording.py`)
- Callback handlers for status and recording details
- Transport params for Plivo WebSocket (`app/ai/voice/agents/breeze_buddy/agent/transport.py`)
- DB migration `013_add_plivo_provider.sql` (PLIVO added to constraints)

### What Exists for Exotel Inbound (Reference Implementation)
- `handle_voicebot_url()` at `GET/POST /exotel/voicebot-url` - single entry point for inbound + outbound (`app/api/routers/breeze_buddy/telephony/inbound/handlers.py`)
- Detects inbound vs outbound by checking if lead exists for `CallSid`
- Template resolution: single template -> direct WebSocket; multiple templates -> IVR mode
- Agent-side IVR: pre-generates TTS audio, caches in Redis, plays menu over WebSocket, waits for DTMF
- `handle_inbound_call()` creates lead on-the-fly for inbound calls (`app/ai/voice/agents/breeze_buddy/agent/inbound.py`)

### Identified Gaps for Plivo Inbound

| # | Gap | Location | Description |
|---|-----|----------|-------------|
| 1 | `handle_plivo_answer` is outbound-only | `callbacks/handlers.py:198` | Returns static WebSocket URL without checking if call is inbound. No template_id, from_number, or ivr_mode query params. |
| 2 | `handle_inbound_call` blocks Plivo | `agent/inbound.py:41` | Hardcoded: `if provider != CallProvider.EXOTEL: return None, "Inbound calls not supported"` |
| 3 | Audio format conversion wrong for Plivo | `agent/ivr.py:394-417` | `_convert_audio_for_provider` treats all non-Twilio as Exotel (PCM). Plivo uses **mulaw** (same as Twilio), since Stream uses `contentType="audio/x-mulaw;rate=8000"`. |
| 4 | Greeting audio format wrong for Plivo | `utils/common.py:~540` | `prepare_initial_greeting_payload` has same issue - converts to PCM for non-Twilio. Plivo needs mulaw. |
| 5 | IVR `_send_audio` uses wrong format for Plivo | `agent/ivr.py:420-439` | Uses Twilio/Exotel `{"event": "media", "streamSid": ...}` format. Plivo bidirectional streaming uses `{"event": "playAudio", "media": {"contentType": ..., "payload": ...}}` format. |
| 6 | No IVR mechanism for Plivo | - | Plivo WebSocket Stream may not forward DTMF events. Agent-side IVR (DTMF over WebSocket) may not work for Plivo. |
| 7 | WebSocket URL missing query params | `callbacks/handlers.py:226` | Static path `/plivo/callback/order-confirmation/v2` without template_id or from_number. |

## Design Decision: Plivo Native IVR vs Agent-Side IVR

### Why Plivo Native IVR (Recommended)

The Exotel Voicebot applet only supports returning a JSON `{"url": "wss://..."}` response - there's no way to do server-side IVR with Exotel's applet. That's why the IVR is done agent-side over WebSocket (play audio, listen for DTMF).

Plivo is different - it supports standard XML elements including `<GetInput>` for DTMF collection and `<Speak>`/`<Play>` for audio. This means we can do IVR **at the XML level** before the WebSocket Stream is established:

| Aspect | Exotel (Agent-Side IVR) | Plivo Native IVR (Proposed) |
|--------|------------------------|----------------------------|
| Where IVR happens | Over WebSocket after Stream starts | In XML before Stream starts |
| DTMF handling | WebSocket event parsing | Plivo handles natively |
| Retry logic | Custom async code | Recursive XML endpoint |
| TTS for menu | Pre-generated, cached in Redis | Plivo `<Speak>` element |
| Complexity | High (audio gen, Redis cache, WS events) | Low (XML response) |
| Reliability | Depends on WS DTMF support | Native Plivo feature |

### Flow Comparison

**Exotel Inbound (existing):**
```
Customer dials -> Voicebot applet -> /exotel/voicebot-url
  -> JSON {"url": "wss://...?ivr_mode=true"}
  -> WebSocket connects -> Agent plays audio -> Waits for DTMF -> Creates lead
```

**Plivo Inbound (proposed):**
```
Customer dials -> Plivo -> POST /plivo/answer
  -> If single template: XML <Stream> with template_id in WS URL
  -> If multiple templates: XML <GetInput><Speak>menu</Speak></GetInput>
     -> DTMF collected -> POST /plivo/ivr-select?attempt=1
     -> XML <Stream> with selected template_id in WS URL
```

## Implementation Plan

### Step 1: Extract Shared Inbound Logic into a Common Helper

**File:** `app/api/routers/breeze_buddy/telephony/inbound/handlers.py` (new helper function)

The core inbound logic already exists in `handle_voicebot_url()` (lines 112-248). Rather than duplicating this in `handle_plivo_answer`, extract the shared lookup logic into a reusable function:

```python
async def resolve_inbound_templates(call_sid: str, from_number: str, to_number: str) -> dict:
    """
    Shared inbound call resolution logic used by both Exotel and Plivo handlers.

    1. Check if lead exists for call_sid (outbound detection)
    2. If outbound: look up template from lead
    3. If inbound: look up outbound_number by to_number, get all templates
    4. Build template_list with IVR descriptions
    5. Resolve voice_name and ivr_greeting from template configurations

    Returns dict with keys:
        is_outbound: bool
        lead: Optional[LeadCallTracker]  (if outbound)
        template: Optional[TemplateModel] (if outbound, single template)
        templates: List[TemplateModel]    (if inbound)
        template_list: List[dict]         (id, name, description)
        voice_name: str
        ivr_greeting: Optional[str]
        error: Optional[str]             (if lookup failed)
        error_status: Optional[int]      (HTTP status for error)
    """
```

Then refactor `handle_voicebot_url()` to call this helper instead of inlining the logic.

### Step 2: Refactor `handle_plivo_answer` to Use Shared Helper

**File:** `app/api/routers/breeze_buddy/telephony/callbacks/handlers.py`

Modify `handle_plivo_answer()` to:

1. Extract `CallUUID`, `From`, `To` from Plivo's POST form data
2. Call `resolve_inbound_templates(call_uuid, from_number, to_number)` from the shared helper
3. **If outbound (lead exists):** Return `<Stream>` XML with WebSocket URL including `template_id` and `from_number` query params
4. **If inbound, single template:** Return `<Stream>` XML with `template_id` and `from_number` in WebSocket URL query params
5. **If inbound, multiple templates:** Return `<GetInput>` XML with `<Speak>` menu and action URL pointing to `/plivo/ivr-select`
6. **If error (no templates/number not configured):** Return `<Speak>` error message + `<Hangup>`
7. Start recording for all calls (existing behavior)

### Step 3: Create Plivo IVR Selection Endpoint

**File:** `app/api/routers/breeze_buddy/telephony/callbacks/handlers.py` (new handler)
**File:** `app/api/routers/breeze_buddy/telephony/callbacks/__init__.py` (new route)

Create `handle_plivo_ivr_select()` endpoint at `POST /plivo/ivr-select`:

1. Receive Plivo's `<GetInput>` callback with `Digits` parameter
2. Accept query params: `attempt` (retry count), `options` (base64-encoded template list JSON), `from_number`, `to_number`
3. Validate digit maps to a valid template option
4. **If valid digit:** Return `<Stream>` XML with selected `template_id` + `from_number` in WebSocket URL. Start recording.
5. **If invalid digit and attempts < 3:** Return `<GetInput>` XML again with `attempt+1`
6. **If invalid digit and attempts >= 3:** Return `<Speak>` goodbye + `<Hangup>`
7. **If no input (timeout):** Same retry logic as invalid digit

Register the new route:
```python
@router.post("/plivo/ivr-select")
async def plivo_ivr_select(request: Request):
    return await handle_plivo_ivr_select(request)
```

### Step 4: Update `handle_inbound_call` to Support Plivo

**File:** `app/ai/voice/agents/breeze_buddy/agent/inbound.py`

Change line 41 from:
```python
if provider != CallProvider.EXOTEL:
```
to:
```python
if provider not in (CallProvider.EXOTEL, CallProvider.PLIVO):
```

### Step 5: Fix Audio Format Conversion for Plivo

Plivo's WebSocket Stream uses `contentType="audio/x-mulaw;rate=8000"`, meaning Plivo expects **mulaw** format (same as Twilio), not PCM (like Exotel).

**File:** `app/ai/voice/agents/breeze_buddy/agent/ivr.py`

Update `_convert_audio_for_provider()`:
```python
if provider_str in ("twilio", "plivo"):
    return mulaw_data  # Both use mulaw
else:
    pcm_data = audioop.ulaw2lin(mulaw_data, 2)
    return pcm_data
```

**File:** `app/ai/voice/agents/breeze_buddy/utils/common.py`

Update `prepare_initial_greeting_payload()` similarly - add `"plivo"` to the Twilio mulaw branch.

### Step 6: Fix IVR Audio Sending for Plivo (WebSocket Event Format)

**File:** `app/ai/voice/agents/breeze_buddy/agent/ivr.py`

Update `_send_audio()` to use Plivo's `playAudio` event format:
```python
async def _send_audio(ws, stream_sid, audio_bytes, provider="exotel"):
    payload = base64.b64encode(audio_bytes).decode("utf-8")
    if provider.lower() == "plivo":
        media_message = {
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": payload,
            },
        }
    else:
        media_message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        }
    await send_message(ws=ws, message=media_message)
```

Note: With the Plivo native IVR approach (Step 3), the IVR happens before the WebSocket is established, so this function won't be called for Plivo IVR. However, it should still be fixed for correctness (initial greeting audio is sent via WebSocket).

### Step 7: Update Greeting Audio Sending for Plivo

**File:** `app/ai/voice/agents/breeze_buddy/agent/utils/` (or wherever `send_initial_greeting` builds WebSocket messages)

Ensure that when sending the initial greeting audio payload over WebSocket for Plivo calls, the `playAudio` event format is used instead of the `media` event format.

### Step 8: Add WebSocket URL Query Parameters for Plivo

**File:** `app/api/routers/breeze_buddy/telephony/callbacks/handlers.py`

In both `handle_plivo_answer` and `handle_plivo_ivr_select`, include query parameters in the WebSocket URL:
```
wss://.../plivo/callback/order-confirmation/v2?template_id={id}&from_number={number}
```

This ensures the agent can extract `template_id` and `from_number` from `custom_parameters` in `call_data` (Pipecat parses WebSocket URL query params as custom_parameters).

## File Changes Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `app/api/routers/breeze_buddy/telephony/inbound/handlers.py` | **Moderate refactor** | Extract shared lookup logic into `resolve_inbound_templates()` helper; refactor `handle_voicebot_url` to use it |
| `app/api/routers/breeze_buddy/telephony/callbacks/handlers.py` | **Major modify** | Refactor `handle_plivo_answer` to use shared helper; add `handle_plivo_ivr_select` |
| `app/api/routers/breeze_buddy/telephony/callbacks/__init__.py` | **Minor modify** | Register new `/plivo/ivr-select` route |
| `app/ai/voice/agents/breeze_buddy/agent/inbound.py` | **Minor modify** | Allow PLIVO provider for inbound calls (line 41) |
| `app/ai/voice/agents/breeze_buddy/agent/ivr.py` | **Minor modify** | Fix `_convert_audio_for_provider` for Plivo (mulaw); fix `_send_audio` for Plivo `playAudio` format |
| `app/ai/voice/agents/breeze_buddy/utils/common.py` | **Minor modify** | Fix greeting audio format for Plivo (mulaw not PCM) |
| `app/ai/voice/agents/breeze_buddy/agent/utils.py` (or similar) | **Minor modify** | Fix greeting WebSocket message format for Plivo (`playAudio` event) |

## Testing Strategy

1. **Outbound calls (regression):** Verify existing Plivo outbound flow still works after refactoring `handle_plivo_answer`
2. **Inbound single template:** Configure a Plivo number with one template -> call it -> verify direct connection
3. **Inbound multiple templates (IVR):** Configure a number with 2+ templates -> call it -> hear menu -> press digit -> verify correct template loads
4. **IVR invalid input:** Press invalid digit -> verify retry menu
5. **IVR timeout:** Don't press anything -> verify retry and eventual goodbye
6. **IVR max retries:** Exhaust all 3 attempts -> verify goodbye message and hangup
7. **Recording:** Verify recordings work for inbound calls
8. **Audio format:** Verify greeting and any agent audio sounds correct (mulaw, not garbled PCM)
