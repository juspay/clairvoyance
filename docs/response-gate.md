# ResponseStateGate - Double-Speaking Prevention

## Overview

`ResponseStateGate` is a pipecat processor that controls how user speech is handled when the bot is actively responding. It sits between STT and the LLM aggregator, tracking the state of TTS playback and applying one of three configurable interruption modes: interrupt the bot, buffer user speech for later, or silently discard it.

---

## The Problem

With `aggregation_timeout=0`, each transcription is sent immediately to the LLM:

```
User: "Hello" → TranscriptionFrame → LLM Request 1
User: "Wait..." → TranscriptionFrame → LLM Request 2
                                    → Both complete → DOUBLE SPEAKING!
```

## The Solution

`ResponseStateGate` tracks whether the bot is actively responding and interrupts the flow when new user speech arrives:

```
Mode: ENABLED (interrupt)
  User: "Hello" → TranscriptionFrame → LLM processing...
  User: "Wait..." → ResponseGate detects active state
                   → Cancels LLM Request 1
                   → Processes "Wait..." only
                   → Single response ✅

Mode: DISABLED_WITH_STORE (buffer)
  User: "Hello" → TranscriptionFrame → LLM processing... → Bot speaking
  User: "Wait..." → ResponseGate buffers it, bot keeps speaking
                   → Bot finishes → flush "Wait..." → LLM processes → Single response ✅

Mode: DISABLED_WITHOUT_STORE (discard)
  User: "Hello" → TranscriptionFrame → LLM processing... → Bot speaking
  User: "Wait..." → ResponseGate discards it, bot keeps speaking
                   → Bot finishes → IDLE, no action
```

---

## Pipeline Position

```
┌─────────────────┐    ┌──────┐    ┌──────────────┐    ┌────┐    ┌────┐    ┌────────┐    ┌─────────────┐
│ transport.input │───►│ STT  │───►│ response_gate│───►│ agg│───►│ LLM │───►│  TTS   │───►│ transport   │
│                 │    │      │    │              │    │user│    │     │    │         │    │   .output() │
└─────────────────┘    └──────┘    └──────────────┘    └────┘    └────┘    └────────┘    └─────────────┘
                              ↑
                    Our processor
```

---

## State Machine

```
                    ┌─────────────────────┐
                    │        IDLE         │◄──────────────────────────┐
                    │  (No active response)│                           │
                    └──────────┬──────────┘                           │
                               │                                       │
                               │ TranscriptionFrame                    │
                               │ (final only; interims dropped)        │
                               ▼                                       │
                    ┌─────────────────────┐                           │
                    │   LLM_PROCESSING    │                           │
                    │  (LLM received ctx) │                           │
                    └──────────┬──────────┘                           │
                               │                                       │
                               │ BotStartedSpeakingFrame               │
                               ▼                                       │
                    ┌─────────────────────┐                           │
                    │        BOTH         │───────────────────────────┘
                    │  (LLM + TTS active) │   BotStoppedSpeakingFrame
                    └──────────┬──────────┘
                               │
                               │ BotStartedSpeakingFrame
                               │ (initial greeting, no LLM)
                    ┌─────────────────────┐
                    │    TTS_SPEAKING     │
                    │  (Audio playing)    │
                    └──────────┬──────────┘
                               │
                               │ BotStoppedSpeakingFrame
                               ▼
                         Back to IDLE
```

> **Note:** `LLMFullResponseStartFrame` / `LLMFullResponseEndFrame` flow
> **downstream** (toward TTS/Output) and never reach ResponseGate, which sits
> upstream of the LLM. State transitions rely solely on
> `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` which flow upstream.

### State Descriptions

| State | Description |
|-------|-------------|
| `IDLE` | No pending response, ready for new input |
| `LLM_PROCESSING` | LLM has received context, generating response |
| `TTS_SPEAKING` | TTS is generating/playing audio |
| `BOTH` | Both LLM and TTS are active |

---

## Frame Flow

### Frames that Change State

| Frame | Effect on State |
|-------|-----------------|
| `TranscriptionFrame` | At IDLE → LLM_PROCESSING (via `_handle_transcription_frame`). At non-IDLE → mode-dependent (interrupt / store / discard) |
| `InterimTranscriptionFrame` | At IDLE → dropped. At non-IDLE → same mode-dependent logic as `TranscriptionFrame` |
| `BotStartedSpeakingFrame` | IDLE → TTS_SPEAKING; LLM_PROCESSING → BOTH |
| `BotStoppedSpeakingFrame` | TTS_SPEAKING → IDLE; BOTH → IDLE (flush stored transcription if DISABLED_WITH_STORE) |

### Frames that Pass Through Unchanged

- `StartFrame`
- `EndFrame`
- `InterruptionFrame`
- `CancelFrame`
- All other frames not listed above

---

## Interruption Logic

### Mode: ENABLED (default)

```
1. TranscriptionFrame or InterimTranscriptionFrame arrives
2. Is state == IDLE?
   YES → Drop if interim, otherwise process normally (state = LLM_PROCESSING)
   NO  → Continue to step 3
3. Push InterruptionFrame upstream (cancels LLM/TTS)
4. Wait for interruption to complete
5. Buffer the new transcription
6. Flush buffered transcription immediately
   → state = LLM_PROCESSING
   → Push frame downstream
```

### Mode: DISABLED_WITH_STORE

```
1. TranscriptionFrame or InterimTranscriptionFrame arrives
2. Is state == IDLE?
   YES → Drop if interim, otherwise process normally (state = LLM_PROCESSING)
   NO  → Continue to step 3
3. Store frame in _buffered_transcription (overwrites previous — safe
   because Soniox interims are cumulative)
4. Return early — bot keeps speaking uninterrupted
5. When BotStoppedSpeakingFrame arrives → state = IDLE
6. Flush buffered transcription (only if it's a final TranscriptionFrame;
   buffered interims are discarded, the final will arrive shortly)
```

### Mode: DISABLED_WITHOUT_STORE

```
1. TranscriptionFrame or InterimTranscriptionFrame arrives
2. Is state == IDLE?
   YES → Drop if interim, otherwise process normally (state = LLM_PROCESSING)
   NO  → Continue to step 3
3. Silently discard the frame
4. Return early — bot keeps speaking, user speech is lost
```

### Key Code Path

```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    # 1. If interruption in progress, buffer transcriptions
    if self._interruption_in_progress and isinstance(
        frame, (TranscriptionFrame, InterimTranscriptionFrame)
    ):
        self._buffered_transcription = frame
        return  # Don't push, wait for interruption to finish

    # 2. Track TTS/Bot speaking state (upstream frames only —
    #    LLMFullResponse frames flow downstream and never reach here)
    if isinstance(frame, BotStartedSpeakingFrame):
        # IDLE → TTS_SPEAKING, LLM_PROCESSING → BOTH
        ...
    elif isinstance(frame, BotStoppedSpeakingFrame):
        # TTS_SPEAKING → IDLE, BOTH → IDLE
        # If DISABLED_WITH_STORE and buffered → flush
        ...

    # 3. Handle transcription with mode-dependent logic
    elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
        if self._state != ResponseState.IDLE:
            if self._interruption_mode == InterruptionMode.ENABLED:
                # Interrupt, buffer, flush
                ...
            elif self._interruption_mode == InterruptionMode.DISABLED_WITH_STORE:
                # Store, return (bot keeps speaking)
                self._buffered_transcription = frame
                return
            else:
                # DISABLED_WITHOUT_STORE — discard
                return

        # State is IDLE — drop interims, process finals
        if isinstance(frame, InterimTranscriptionFrame):
            return
        await self._handle_transcription_frame(frame, direction)
        return
```

---

## Why Buffer During Interruption?

### Race Condition Without Buffering

```
Without buffering:
─────────────────────────────
T0: New STT arrives during active response
T0: push_interruption() called
T0: Interruption starts propagating upstream
T0: Current process_frame continues → push_frame(new_stt) ❌ DUPLICATE!
    → Old response + new response both complete → DOUBLE SPEAKING
```

### With Buffering

```
With buffering:
─────────────────────────────
T0: New STT arrives during active response
T0: push_interruption() called
T0: Interruption starts propagating upstream
T0: Current process_frame buffers new_stt, returns early ❌ NOTHING PUSHED
T1: Interruption completes (LLM/TTS cancelled)
T1: _flush_buffered_transcription()
T1: push_frame(new_stt) ✅ ONLY NEW REQUEST
    → Only new response completes → NO DOUBLE SPEAKING
```

---

## Scenarios Handled

| Scenario | Mode | State Before | Action | Result |
|----------|------|--------------|--------|--------|
| Normal flow | Any | IDLE | Process final transcription normally | Single response |
| Interim at IDLE | Any | IDLE | Dropped (final will follow) | No premature LLM call |
| User speaks while bot active | ENABLED | BOTH / TTS_SPEAKING | Cancel LLM+TTS, process new | New response only |
| Quick double STT | ENABLED | LLM_PROCESSING | Buffer 2nd, flush after 1st | Latest only |
| User speaks while bot active | DISABLED_WITH_STORE | BOTH / TTS_SPEAKING | Buffer transcription, bot keeps speaking | Flushed when bot finishes |
| User speaks while bot active | DISABLED_WITHOUT_STORE | BOTH / TTS_SPEAKING | Discard transcription, bot keeps speaking | User speech lost |
| Bot stops with buffered interim | DISABLED_WITH_STORE | BOTH → IDLE | Discard interim, wait for final | No partial LLM call |
| Bot stops with buffered final | DISABLED_WITH_STORE | BOTH → IDLE | Flush final transcription | Single response |

---

## Configuration

### Template Configuration

Interruption behavior is controlled per-template via `interruption_config`:

| Mode | JSON value | Behavior |
|------|------------|----------|
| `ENABLED` (default) | `"enabled"` | Interrupt bot, buffer user speech, process after interruption |
| `DISABLED_WITH_STORE` | `"disabled_with_store"` | Don't interrupt; buffer user speech, flush when bot finishes |
| `DISABLED_WITHOUT_STORE` | `"disabled_without_store"` | Don't interrupt; silently discard user speech |

### Code Configuration

```python
from app.ai.voice.agents.breeze_buddy.processors import ResponseStateGate
from app.ai.voice.agents.breeze_buddy.template.types import InterruptionMode

# The response gate is always active, driven by the template's interruption_config.
interruption_config = getattr(configurations, "interruption_config", None)
interruption_mode = (
    interruption_config.mode if interruption_config else InterruptionMode.ENABLED
)
response_gate = ResponseStateGate(interruption_mode=interruption_mode)

pipeline_parts = [
    transport.input(),
    stt,
    transcription_gate,
    response_gate,
    context_aggregator.user(),
    llm,
    tts,
    transport.output(),
    context_aggregator.assistant(),
]

pipeline = Pipeline(pipeline_parts)
```

---

## Logging

All logs avoid PII (no raw transcription text):

| Log Level | Message | Example |
|-----------|---------|---------|
| INFO | Processing transcription | `ResponseGate: Processing transcription (id=140612345678, length=25)` |
| INFO | Interruption triggered | `ResponseGate: Interrupting state=BOTH for new transcription (id=140612345678, length=25)` |
| DEBUG | State transitions | `ResponseGate: LLM_PROCESSING`, `ResponseGate: BOTH`, etc. |
| DEBUG | Buffering during interrupt | `ResponseGate: Buffering new transcription during interruption (id=..., length=...)` |

### Log Fields (No PII)

- `id` - Object identity (Python `id()`) for tracking
- `length` - Character count of text
- `state` - State machine state (IDLE, LLM_PROCESSING, etc.)

---

## Debugging Tips

### Enable Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Key Log Messages to Watch

```
# Normal flow (ENABLED or IDLE):
ResponseGate: Processing transcription (id=..., length=...)
ResponseGate: LLM_PROCESSING
ResponseGate: BOTH
ResponseGate: IDLE (bot finished speaking)

# Interruption (ENABLED mode):
ResponseGate: Interrupting state=BOTH for new transcription (id=..., length=...)
ResponseGate: Processing transcription (id=..., length=...)

# Store mode:
ResponseGate: Storing transcription while bot active (mode=disabled_with_store, state=BOTH)
ResponseGate: Bot finished speaking, flushing stored transcription

# Discard mode:
ResponseGate: Discarding transcription while bot active (mode=disabled_without_store, state=BOTH)

# Interim handling:
ResponseGate: Discarding buffered interim transcription, waiting for final
```

### Expected Behavior

1. **Single transcription → Single log**:
   ```
   Processing transcription → LLM_PROCESSING → ... → IDLE
   ```

2. **With interruption → Two logs**:
   ```
   Processing transcription → LLM_PROCESSING → ...
   Interrupting state=BOTH → ...
   Processing transcription → LLM_PROCESSING → ... → IDLE
   ```

---

## Architecture

### Class: ResponseStateGate

```python
class ResponseStateGate(FrameProcessor):
    def __init__(self, interruption_mode: InterruptionMode = InterruptionMode.ENABLED, **kwargs):
        self._state = ResponseState.IDLE
        self._buffered_transcription: TranscriptionFrame | InterimTranscriptionFrame | None = None
        self._interruption_in_progress = False
        self._interruption_mode = interruption_mode

    async def _handle_transcription_frame(
        self, frame: TranscriptionFrame | InterimTranscriptionFrame, direction: FrameDirection
    ):
        """Process a transcription frame (buffered or fresh)."""
        ...

    async def _flush_buffered_transcription(self, direction: FrameDirection):
        """Flush any buffered transcription. Discards InterimTranscriptionFrames
        (the final will arrive shortly and be processed at IDLE)."""
        ...
```

### Methods

| Method | Purpose |
|--------|---------| 
| `process_frame()` | Main entry point, handles all frame types |
| `_handle_transcription_frame()` | Unified handler for final and interim transcription frames |
| `_flush_buffered_transcription()` | Process buffered frame after bot stops speaking (skips interims) |

---

## Integration with aggregation_timeout

The response gate works **with** `aggregation_timeout=0`:

```
Without ResponseGate:
  aggregation_timeout=0 → Multiple LLM requests → Double speaking ❌

With ResponseGate:
  aggregation_timeout=0 → ResponseGate buffers → Single LLM request → No double speaking ✅
```

The response gate intercepts new transcriptions that would otherwise trigger separate LLM requests, ensuring only the latest transcription proceeds.

---

## Troubleshooting

### Double Speaking Still Occurring

1. Verify response_gate is in the pipeline
2. Check the template's `interruption_config.mode` is set correctly
3. Check logs for "Interrupting state=..." messages
4. Ensure no other processor is bypassing the gate

### Interruption Not Triggering

1. Check state transitions in logs
2. Verify `BotStartedSpeakingFrame` and `BotStoppedSpeakingFrame` are flowing
3. Check for missing frame handlers

### Stalled Buffer

Buffer should flush when the bot finishes speaking. If not:
1. Check `_flush_buffered_transcription()` is called on `BotStoppedSpeakingFrame`
2. Verify the buffered frame is a final `TranscriptionFrame` (interims are intentionally discarded)
3. Verify no exception in the flush path
4. Check that mode is `DISABLED_WITH_STORE` (other modes don't flush)

---

## Files

| File | Purpose |
|------|---------|
| `app/ai/voice/agents/breeze_buddy/processors/response_gate.py` | Main processor implementation |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` | Pipeline integration |
| `app/ai/voice/agents/breeze_buddy/template/types.py` | `InterruptionMode` and `InterruptionConfig` definitions |
| `docs/aggregation-timeout.md` | Related: aggregation timeout explanation |
