# Daily RTVI Events Reference

Real-time events emitted by the Breeze Buddy backend to Daily-connected clients via the RTVI protocol. These events are available in all Daily execution modes (`DAILY`, `DAILY_TEST`, `DAILY_STREAM`).

## Event Summary

| Event | What triggers it | When it fires | Typical use case |
|-------|-----------------|---------------|------------------|
| **Connection & Transport** | | | |
| `onConnected` | WebRTC connection established with Daily room | After `client.connect()` succeeds | Enable UI controls, show "connected" state |
| `onDisconnected` | Client leaves room or connection drops | On `client.disconnect()`, network loss, or bot-initiated disconnect | Clean up UI, save state, navigate away |
| `onTransportStateChanged` | Internal state machine transitions | Every state change during connect/disconnect lifecycle | Show detailed connection progress (loading spinners, status text) |
| `onError` | Transport failure, RTVI protocol error, or fatal pipeline error | Any time — network issues, invalid config, pipeline crash | Show error message, retry logic. Fatal errors auto-disconnect |
| `onMessageError` | Server rejects a client message | When `sendClientMessage()` gets an error response | Debug failed commands, show user feedback |
| **Participants** | | | |
| `onParticipantJoined` | A user or bot joins the Daily room | After WebRTC negotiation completes for each participant | Show participant list, detect bot arrival |
| `onParticipantLeft` | A user or bot leaves the room | On disconnect, timeout, or explicit leave | Update participant list, detect bot departure |
| **Bot Lifecycle** | | | |
| `onBotStarted` | Backend spawns the bot process | After `/connect` API creates the bot — before it joins the room | Show "starting bot..." indicator |
| `onBotConnected` | Bot's Daily transport joins the room | Bot is in the room but pipeline may not be ready yet | Show "bot joining..." indicator |
| `onBotReady` | Bot pipeline fully initialized (STT, TTS, LLM all ready) | After all services are loaded and FlowManager is initialized | **Primary "ready" signal** — safe to speak, send TTS, start conversation |
| `onBotDisconnected` | Bot leaves the room | Conversation ended (normal or timeout), pipeline error, or explicit disconnect | Trigger post-call flow: save data, show summary, navigate to results |
| **User Speech** | | | |
| `onUserStartedSpeaking` | Silero VAD detects user voice activity above confidence threshold | ~100ms after user begins speaking (VAD start_secs) | Show "listening" indicator, pause UI animations |
| `onUserStoppedSpeaking` | VAD detects sustained silence after speech | After VAD stop_secs of silence following speech | Hide "listening" indicator, show "processing" state |
| `onUserTranscript` | STT provider returns text from user's speech | Continuously during speech (interim) and at end of utterance (final) | Display live captions. `final: false` = interim, `final: true` = confirmed text |
| `onUserMuteStarted` | Pipeline mutes user input (e.g., during bot speech with interruption disabled) | When interruption mode is `disabled_discard` and bot starts speaking | Show "muted" indicator |
| `onUserMuteStopped` | Pipeline unmutes user input | When bot stops speaking and user is allowed to interrupt again | Hide "muted" indicator |
| **Bot Speech** | | | |
| `onBotStartedSpeaking` | Transport output begins sending TTS audio frames | When first audio frame from TTS reaches the output transport | Show "speaking" animation, disable send button |
| `onBotStoppedSpeaking` | Transport output finishes sending all TTS audio | When last audio frame has been played out | Hide "speaking" animation, re-enable controls |
| `onBotTtsStarted` | TTS service begins synthesizing text to audio | Before audio frames are generated — synthesis begins | Show "synthesizing" indicator (precedes `onBotStartedSpeaking`) |
| `onBotTtsText` | TTS service receives text to synthesize | For each text chunk sent to TTS | Display what the bot is about to say |
| `onBotTtsStopped` | TTS service finishes synthesis for current utterance | After all text has been converted to audio | Hide "synthesizing" indicator |
| `onBotOutput` | Aggregated bot output (word or sentence level) | As TTS text is aggregated by the pipeline | Build bot response text incrementally. `spoken: true` means audio was generated |
| `onBotTranscript` | *(deprecated)* Legacy bot transcription | Same as `onBotOutput` | Use `onBotOutput` instead |
| **Bot LLM** *(full agent mode only)* | | | |
| `onBotLlmStarted` | LLM begins generating a response | After user turn ends and context is sent to LLM | Show "thinking..." indicator |
| `onBotLlmText` | LLM streams a text chunk | For each token/word the LLM generates | Display streaming response text |
| `onBotLlmStopped` | LLM finishes generating | After the full response is complete | Hide "thinking" indicator |
| `onBotLlmSearchResponse` | LLM returns a search/retrieval result | When LLM uses RAG or search tools | Display search results or citations |
| **Function Calls** *(full agent mode only)* | | | |
| `onLLMFunctionCallStarted` | LLM decides to call a template function | When LLM output includes a function call | Show "performing action..." indicator |
| `onLLMFunctionCallInProgress` | Function is executing with arguments | During async function execution (e.g., API call, DB lookup) | Display function name + args for transparency |
| `onLLMFunctionCallStopped` | Function returns result or is cancelled | After function handler completes | Show result, update UI with extracted data (e.g., order confirmed) |
| **Audio Levels** | | | |
| `onLocalAudioLevel` | Mic audio analyzed for volume | ~6.7 Hz (every 150ms) while mic is active | Render mic volume meter / waveform visualization |
| `onRemoteAudioLevel` | Bot audio analyzed for volume | ~6.7 Hz while bot audio is active | Render bot volume meter / speaking animation intensity |
| **Metrics** | | | |
| `onMetrics` | Pipeline emits performance data | After TTS synthesis, LLM generation, or STT processing completes | Display latency stats, monitor TTFB, log for analytics |
| **Audio Tracks** | | | |
| `onTrackStarted` | WebRTC audio/video track becomes available | When a participant's media stream is ready | **Attach bot audio to `<audio>` element for playback** |
| `onTrackStopped` | WebRTC track is removed | When participant leaves or stops streaming | Clean up audio element |
| `onScreenTrackStarted` | Screen share track starts | When a participant shares their screen | Display screen share video |
| `onScreenTrackStopped` | Screen share track stops | When screen sharing ends | Remove screen share video |
| `onScreenShareError` | Screen share request fails | Permission denied or browser limitation | Show error message |
| **Device Management** | | | |
| `onAvailableMicsUpdated` | System mic list changes | On USB device plug/unplug, Bluetooth connect/disconnect | Update mic selector dropdown |
| `onAvailableSpeakersUpdated` | System speaker list changes | On device plug/unplug | Update speaker selector dropdown |
| `onAvailableCamsUpdated` | System camera list changes | On device plug/unplug | Update camera selector dropdown |
| `onMicUpdated` | Active mic changes | After `client.updateMic()` or system default changes | Confirm mic switch in UI |
| `onSpeakerUpdated` | Active speaker changes | After `client.updateSpeaker()` | Confirm speaker switch |
| `onCamUpdated` | Active camera changes | After `client.updateCam()` | Confirm camera switch |
| `onDeviceError` | Device access fails | Permission denied, device in use, hardware error | Show "mic access denied" or "device unavailable" message |
| **Server Messages** | | | |
| `onServerMessage` | Backend sends custom RTVI event via `_emit_rtvi_event()` | On conversation-start, conversation-end, pipeline-error, bot-ready, function-approval-request, function-approval-resolved | Handle Breeze Buddy-specific lifecycle events + HITL approval cards |

## Event Timeline

Typical session event order:

```
onTransportStateChanged("initializing")
onTransportStateChanged("initialized")
onTransportStateChanged("connecting")
onParticipantJoined (local user)
onConnected
onTransportStateChanged("connected")
onTrackStarted (local mic track)
onAvailableMicsUpdated
onBotStarted
onParticipantJoined (bot)
onBotConnected
onTrackStarted (bot audio track)  ← attach to <audio> element here
onBotReady                        ← session is live
onServerMessage("conversation-start")
  │
  ├─ onUserStartedSpeaking        ← user talks
  ├─ onUserTranscript (interim)
  ├─ onUserTranscript (final)
  ├─ onUserStoppedSpeaking
  │
  ├─ onBotLlmStarted              ← LLM thinks (full agent mode only)
  ├─ onBotLlmText (streaming)
  ├─ onBotLlmStopped
  │
  ├─ onBotTtsStarted              ← TTS synthesizes
  ├─ onBotTtsText
  ├─ onBotStartedSpeaking         ← audio plays
  ├─ onBotOutput
  ├─ onBotTtsStopped
  ├─ onBotStoppedSpeaking         ← audio done
  ├─ onMetrics (TTFB, processing)
  │
  └─ ... (repeat for each turn)
  │
onServerMessage("conversation-end")
onBotDisconnected
onParticipantLeft (bot)
onDisconnected
onTransportStateChanged("disconnected")
```

## Prerequisites

- `ENABLE_BREEZE_BUDDY_DAILY_EVENTS=True` in environment
- Client connects via PipecatClient + DailyTransport (recommended) or raw Daily SDK

## Quick Start

### 1. Install SDK

```bash
npm install @pipecat-ai/client-js @pipecat-ai/daily-transport
```

### 2. Connect

```typescript
import { PipecatClient } from '@pipecat-ai/client-js';
import { DailyTransport } from '@pipecat-ai/daily-transport';

const client = new PipecatClient({
  transport: new DailyTransport({
    inputSettings: {
      audio: {
        processor: { type: 'noise-cancellation' },
        settings: {
          echoCancellation: { ideal: true },
          noiseSuppression: { ideal: true },
          autoGainControl: { ideal: true },
        },
      },
    },
  }),
  enableMic: true,
  enableCam: false,
  callbacks: {
    // register event handlers here (see below)
  },
});

// room_url and token come from POST /connect response
await client.connect({ url: room_url, token: token });
```

---

## Event Reference

### Connection & Transport

#### `onConnected`
Fired when WebRTC connection to the Daily room is established.
```typescript
onConnected: () => {
  console.log('Connected to Daily room');
}
```

#### `onDisconnected`
Fired when the client leaves or is disconnected from the room.
```typescript
onDisconnected: () => {
  console.log('Disconnected from Daily room');
}
```

#### `onTransportStateChanged`
Fired on every transport state transition.

States: `disconnected` → `initializing` → `initialized` → `authenticating` → `authenticated` → `connecting` → `connected` → `ready` → `disconnecting` → `disconnected`
```typescript
onTransportStateChanged: (state: string) => {
  console.log('Transport state:', state);
}
```

#### `onError`
Fired on transport or RTVI protocol errors.
```typescript
onError: (error: { type?: string; data?: { message: string; fatal: boolean } }) => {
  console.error('Error:', error?.data?.message || error);
  if (error?.data?.fatal) {
    // fatal errors auto-disconnect
  }
}
```

#### `onMessageError`
Fired when a client message gets an error response from the server.
```typescript
onMessageError: (error) => {
  console.error('Message error:', error);
}
```

---

### Participants

#### `onParticipantJoined`
Fired when a participant (user or bot) joins the room.
```typescript
onParticipantJoined: (participant: { id: string; local: boolean }) => {
  console.log('Joined:', participant.id, participant.local ? '(local)' : '(remote)');
}
```

#### `onParticipantLeft`
Fired when a participant leaves.
```typescript
onParticipantLeft: (participant: { id: string }) => {
  console.log('Left:', participant.id);
}
```

---

### Bot Lifecycle

#### `onBotStarted`
Fired after the backend successfully creates the bot process.
```typescript
onBotStarted: (data) => {
  console.log('Bot process started');
}
```

#### `onBotConnected`
Fired when the bot joins the Daily room.
```typescript
onBotConnected: (data) => {
  console.log('Bot joined room');
}
```

#### `onBotReady`
Fired when the bot pipeline is fully initialized and ready to process audio. This is the signal that the session is live.
```typescript
onBotReady: (data: { version?: string }) => {
  console.log('Bot ready, version:', data?.version);
  // safe to start speaking / sending TTS
}
```

#### `onBotDisconnected`
Fired when the bot leaves the room (conversation ended, idle timeout, or error).
```typescript
onBotDisconnected: (data) => {
  console.log('Bot disconnected');
  // PipecatClient auto-disconnects by default (disconnectOnBotDisconnect: true)
}
```

---

### User Speech (STT / VAD)

#### `onUserStartedSpeaking`
Fired when VAD detects the user began speaking.
```typescript
onUserStartedSpeaking: () => {
  console.log('User started speaking');
}
```

#### `onUserStoppedSpeaking`
Fired when VAD detects the user stopped speaking.
```typescript
onUserStoppedSpeaking: () => {
  console.log('User stopped speaking');
}
```

#### `onUserTranscript`
Fired for both interim (partial) and final transcriptions from STT.
```typescript
onUserTranscript: (data: { text: string; final: boolean; user_id?: string; timestamp?: number }) => {
  if (data.final) {
    console.log('Final transcript:', data.text);
  } else {
    console.log('Interim:', data.text);
  }
}
```

#### `onUserMuteStarted`
Fired when the user's audio is muted (by the pipeline or client).
```typescript
onUserMuteStarted: () => {
  console.log('User muted');
}
```

#### `onUserMuteStopped`
Fired when the user's audio is unmuted.
```typescript
onUserMuteStopped: () => {
  console.log('User unmuted');
}
```

---

### Bot Speech (TTS)

#### `onBotStartedSpeaking`
Fired when the bot begins playing TTS audio.
```typescript
onBotStartedSpeaking: () => {
  console.log('Bot started speaking');
}
```

#### `onBotStoppedSpeaking`
Fired when the bot finishes playing TTS audio.
```typescript
onBotStoppedSpeaking: () => {
  console.log('Bot stopped speaking');
}
```

#### `onBotTtsStarted`
Fired when TTS synthesis begins (before audio starts playing).
```typescript
onBotTtsStarted: () => {
  console.log('TTS synthesis started');
}
```

#### `onBotTtsText`
Fired with the text being synthesized by TTS.
```typescript
onBotTtsText: (data: { text: string }) => {
  console.log('TTS text:', data.text);
}
```

#### `onBotTtsStopped`
Fired when TTS synthesis completes.
```typescript
onBotTtsStopped: () => {
  console.log('TTS synthesis finished');
}
```

#### `onBotOutput`
Aggregated bot output text with metadata.
```typescript
onBotOutput: (data: { text: string; spoken: boolean; aggregated_by: 'word' | 'sentence' }) => {
  console.log('Bot output:', data.text, data.spoken ? '(spoken)' : '(text-only)');
}
```

#### `onBotTranscript` *(deprecated)*
Legacy bot transcription event. Use `onBotOutput` instead.
```typescript
onBotTranscript: (data) => {
  console.log('Bot transcript:', data);
}
```

---

### Bot LLM (full agent mode only)

These events fire only when an LLM is in the pipeline (not in `DAILY_STREAM` mode).

#### `onBotLlmStarted`
Fired when the LLM begins generating a response.
```typescript
onBotLlmStarted: () => {
  console.log('LLM generation started');
}
```

#### `onBotLlmText`
Fired for each text chunk streamed from the LLM.
```typescript
onBotLlmText: (data: { text: string }) => {
  console.log('LLM text chunk:', data.text);
}
```

#### `onBotLlmStopped`
Fired when the LLM finishes generating.
```typescript
onBotLlmStopped: () => {
  console.log('LLM generation finished');
}
```

#### `onBotLlmSearchResponse`
Fired when the LLM returns a search/retrieval response.
```typescript
onBotLlmSearchResponse: (data) => {
  console.log('LLM search response:', data);
}
```

---

### Function Calls (full agent mode only)

Reported when the LLM invokes template functions. Report level is `FULL` (includes function names, arguments, and results).

#### `onLLMFunctionCallStarted`
Fired when the LLM begins a function call.
```typescript
onLLMFunctionCallStarted: (data: { function_name?: string }) => {
  console.log('Function call started:', data.function_name);
}
```

#### `onLLMFunctionCallInProgress`
Fired when the function is executing. Includes arguments.
```typescript
onLLMFunctionCallInProgress: (data: {
  function_name?: string;
  tool_call_id?: string;
  arguments?: Record<string, unknown>;
}) => {
  console.log('Function executing:', data.function_name, data.arguments);
}
```

#### `onLLMFunctionCallStopped`
Fired when the function call completes or is cancelled.
```typescript
onLLMFunctionCallStopped: (data: {
  function_name?: string;
  tool_call_id?: string;
  cancelled?: boolean;
  result?: unknown;
}) => {
  console.log('Function done:', data.function_name, data.cancelled ? '(cancelled)' : '');
}
```

---

### Audio Levels

Fired at ~6.7Hz (every 150ms). Useful for visualizing audio waveforms. These are high-frequency — avoid heavy DOM updates in the handler.

#### `onLocalAudioLevel`
User's microphone audio level (0.0 – 1.0).
```typescript
onLocalAudioLevel: (level: number) => {
  // update mic volume indicator
  micBar.style.width = (level * 100) + '%';
}
```

#### `onRemoteAudioLevel`
Bot's audio output level (0.0 – 1.0).
```typescript
onRemoteAudioLevel: (level: number, participant) => {
  // update bot volume indicator
  botBar.style.width = (level * 100) + '%';
}
```

---

### Metrics

Performance metrics emitted periodically.

#### `onMetrics`
```typescript
onMetrics: (data: {
  ttfb?: Array<{ processor: string; value: number; model?: string }>;
  processing?: Array<{ processor: string; value: number }>;
  characters?: Array<{ processor: string; value: number }>;
}) => {
  data.ttfb?.forEach(m => console.log(`TTFB ${m.processor}: ${m.value.toFixed(3)}s`));
  data.processing?.forEach(m => console.log(`Processing ${m.processor}: ${m.value.toFixed(3)}s`));
}
```

---

### Audio Tracks

Low-level track events for managing audio playback.

#### `onTrackStarted`
Fired when an audio/video track becomes available. **This is where you attach bot audio to your speakers.**
```typescript
onTrackStarted: (track: MediaStreamTrack, participant: { local: boolean }) => {
  if (track.kind === 'audio' && !participant.local) {
    const audio = document.createElement('audio');
    audio.autoplay = true;
    audio.setAttribute('playsinline', '');
    audio.srcObject = new MediaStream([track]);
    document.body.appendChild(audio);
  }
}
```

#### `onTrackStopped`
Fired when a track is removed.
```typescript
onTrackStopped: (track: MediaStreamTrack, participant: { local: boolean }) => {
  if (track.kind === 'audio' && !participant.local) {
    audioElement.srcObject = null;
  }
}
```

---

### Device Management

#### `onAvailableMicsUpdated`
```typescript
onAvailableMicsUpdated: (mics: Array<{ deviceId: string; label: string }>) => {
  console.log('Available mics:', mics);
}
```

#### `onAvailableSpeakersUpdated`
```typescript
onAvailableSpeakersUpdated: (speakers) => {
  console.log('Available speakers:', speakers);
}
```

#### `onMicUpdated` / `onSpeakerUpdated` / `onCamUpdated`
Fired when the active device changes.

#### `onDeviceError`
```typescript
onDeviceError: (error: { devices: string[]; type: string; message?: string }) => {
  console.error('Device error:', error.type, error.devices);
}
```

---

### Server Messages (Custom)

Custom events sent by the Breeze Buddy backend via `_emit_rtvi_event()`.

#### `onServerMessage`
```typescript
onServerMessage: (message: { type: string; timestamp?: number; payload?: any }) => {
  switch (message.type) {
    case 'conversation-start':
      console.log('Conversation started');
      break;
    case 'conversation-end':
      console.log('Conversation ended:', message.payload?.reason);
      // reasons: "client_disconnected", "idle_timeout"
      break;
    case 'pipeline-error':
      console.error('Pipeline error:', message.payload?.processor, message.payload?.error);
      break;
    case 'function-approval-request':
      // HITL: the LLM called an approval-gated function (template
      // `approval` config). Show an approve/deny card; answer with the
      // `function-approval-decision` client message below. Re-emitted
      // for still-pending requests when a client (re)connects.
      // payload: { approval_id, function_name, arguments, prompt, timeout_secs }
      break;
    case 'function-approval-resolved':
      // HITL: a pending approval resolved — dismiss its card.
      // payload: { approval_id, status }
      // status: "approved" | "denied" | "timeout" | "cancelled" | "superseded"
      break;
  }
}
```

### HITL function approvals (client → server)

Answer a `function-approval-request` with:

```typescript
client.sendClientMessage('function-approval-decision', {
  approval_id: '...',   // from the request payload
  approved: true,       // or false
  reason: 'optional',   // shown to the LLM on denial
});
```

Semantics:
- Decisions are idempotent per `approval_id` — duplicates and decisions
  arriving after the wait timed out are ignored (the bot logs a stale-id
  warning).
- Whether the bot keeps talking while waiting is the function's existing
  `cancel_on_interruption`: `true` = the bot blocks silently (and ANY user
  utterance cancels the request → `status: "cancelled"`); `false` = async
  call, the bot keeps conversing and the decision may arrive minutes later.
- On timeout/deny the LLM receives `{"status": "denied"|"timeout", reason}`
  as the tool result. Pending requests are denied automatically on client
  disconnect, idle timeout, and conversation end.
- A duplicate request for the same function supersedes the older pending
  one (`status: "superseded"`).
- Voice HITL requires RTVI (`ENABLE_BREEZE_BUDDY_DAILY_EVENTS=true`);
  without it, gated calls on Daily are denied
  (`approval_channel_unavailable`). Telephony has no approval surface —
  the template's `approval.on_no_channel` decides.

---

## DAILY_STREAM Mode

In `DAILY_STREAM` mode, the pipeline runs STT + TTS without an LLM, but still includes the shared `LLMUserAggregator` so clients get high-quality turn events driven by the template's configured VAD + turn strategies. The client controls TTS output.

Pipeline:
```
transport.input() → STT → TranscriptionGate → LLMUserAggregator
                  → TranscriptCollector → TTS → transport.output()
```

The aggregator still pushes `LLMContextFrame`s downstream; they pass through TTS (ignored, not `TTSSpeakFrame`) and die at `transport.output()`. Harmless — no LLM is invoked.

### Sending TTS text

```typescript
client.sendClientMessage('tts-speak', { text: 'Hello, how are you?' });
```

### Events available in stream mode

| Event | Available | Notes |
|-------|-----------|-------|
| `onUserTranscript` | Yes | STT transcription (interim + final) |
| `onUserStartedSpeaking` | Yes | VAD + turn-start strategies (Transcription/MinWords) |
| `onUserStoppedSpeaking` | Yes | Configured stop strategy (SmartTurn / AccumulatingTimeout) |
| `onUserMuteStarted/Stopped` | Yes | When `interruption.mode=disabled_discard` + client TTS active |
| `onBotStartedSpeaking` | Yes | TTS audio playing |
| `onBotStoppedSpeaking` | Yes | TTS audio finished |
| `onBotTtsStarted/Text/Stopped` | Yes | TTS synthesis events |
| `onBotOutput` | Yes | Aggregated TTS text |
| `onBotLlmStarted/Text/Stopped` | No | No LLM in pipeline |
| `onLLMFunctionCall*` | No | No LLM in pipeline |
| `onLocalAudioLevel` | Yes | Mic level |
| `onRemoteAudioLevel` | Yes | Bot audio level |
| `onMetrics` | Yes | TTS TTFB, processing time |

### Template config honored in stream mode

Stream mode reuses `build_pipeline(mode="stream")`, so the same template config drives turn behavior as full agent mode:

| Config | Used? | Why |
|--------|-------|-----|
| `stt_configuration.turn_detection` (`stt_native` / `smart_turn` / `timeout`) | Yes | Drives user-stop strategy |
| `stt_configuration.user_speech_timeout` | Yes | Timeout threshold (only when `turn_detection=timeout`) |
| `stt_configuration.smart_turn` (cpu_count, stop_secs, pre_speech_ms, max_duration_secs) | Yes | SmartTurn ML params (Whisper ONNX) |
| `interruption.mode` (`enabled` / `disabled_discard`) | Yes | Engages `AlwaysUserMuteStrategy` if `disabled_discard` |
| `interruption.min_words` | Yes | Gates turn-start via `MinWordsUserTurnStartStrategy` |
| VAD on/off | Global flag | Controlled by Redis `BREEZE_BUDDY_ENABLE_VAD`, not template. Same behavior as full Daily agent mode |
| VAD params (confidence/start_secs/stop_secs/min_volume) | Template > Redis | Template `configurations.vad_config` overrides per-field, falling back to Redis `BB_DAILY_VAD_*` defaults. Applies to all Daily modes (agent + stream) |
| Fallback when VAD is off | Yes | Turn start/stop events still fire via Transcription / MinWords / SmartTurn / Timeout strategies — later than VAD (on first transcript), but functional |
| `keyword_filter` | Yes | `TranscriptionGateProcessor` filters filler words pre-aggregator |
| Noise filter (AI Coustics) | Yes | Transport-level audio filter |
| TTS voice config (Cartesia/ElevenLabs/Sarvam) | Yes | Client-driven TTS uses configured voice |
| `user_idle_configuration` | **Skipped** | Client drives idle logic itself |
| LLM configuration | **Skipped** | No LLM in stream mode |

**Note on SmartTurn CPU cost**: if the template enables `smart_turn`, stream mode pays the same CPU cost as full agent mode (Whisper ONNX inference per user turn).

### What's NOT in stream mode

| Feature | Reason |
|---------|--------|
| LLM generation | Not created; no `onBotLlmStarted/Text/Stopped` events |
| Function calls | No LLM to invoke them |
| Assistant context aggregator | Only useful when LLM produces responses |
| `UserIdleProcessor` | Skipped so the client can own idle logic |

**Design intent**: client drives conversation content (what to say via `tts-speak`), backend drives conversation mechanics (VAD, turn detection, transcription, TTS synthesis) using the same template config as a full agent.

### Stream mode flow

```
1. POST /leads  →  { execution_mode: "DAILY_STREAM", ... }
2. POST /connect  →  { room_url, token }
3. client.connect({ url, token })
4. Listen: onUserTranscript, onBotStartedSpeaking, etc.
5. Send: client.sendClientMessage('tts-speak', { text: '...' })
6. Disconnect → transcription stored in DB
```

---

## Backend Configuration

RTVI observer params are configured in `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`:

```python
RTVIObserverParams(
    user_transcription_enabled=True,
    user_speaking_enabled=True,
    user_mute_enabled=True,
    user_audio_level_enabled=True,
    bot_llm_enabled=True,
    bot_tts_enabled=True,
    bot_speaking_enabled=True,
    bot_output_enabled=True,
    bot_audio_level_enabled=True,
    metrics_enabled=True,
    function_call_report_level={"*": RTVIFunctionCallReportLevel.FULL},
)
```

Requires `ENABLE_BREEZE_BUDDY_DAILY_EVENTS=True` in environment.

## Demo

See `examples/stream_demo.html` for a working single-file demo with all events wired.
