# ResponseStateGate - Double-Speaking Prevention

## Overview

`ResponseStateGate` is a pipecat processor that prevents **double-speaking** when users interrupt the bot mid-response. It sits between STT and the LLM aggregator, tracking the state of LLM/TTS processing and interrupting active responses when new user speech arrives.

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
User: "Hello" → TranscriptionFrame → LLM processing...
User: "Wait..." → ResponseGate detects active state
                 → Cancels LLM Request 1
                 → Processes "Wait..." only
                 → Single response ✅
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
                               ▼                                       │
                    ┌─────────────────────┐                           │
     ┌─────────────►│   LLM_PROCESSING    │                           │
     │              │  (LLM received ctx) │                           │
     │              └──────────┬──────────┘                           │
     │                         │                                       │
     │                         │ BotStartedSpeakingFrame               │
     │                         ▼                                       │
     │              ┌─────────────────────┐     TranscriptionFrame     │
     │              │        BOTH         │─────(interrupt)────────────┘
     │              │  (LLM + TTS active) │
     │              └──────────┬──────────┘
     │                         │
     │                         │ BotStoppedSpeakingFrame
     │                         ▼
     │              ┌─────────────────────┐
     │              │    TTS_SPEAKING     │◄────────────────────────────┘
     │              │  (Audio playing)    │   TranscriptionFrame
     │              └─────────────────────┘
     │                         │
     └─────────────────────────┘
```

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
| `TranscriptionFrame` | If state != IDLE → interrupt, buffer, flush |
| `LLMFullResponseStartFrame` | IDLE → LLM_PROCESSING; TTS_SPEAKING → BOTH |
| `LLMFullResponseEndFrame` | BOTH → TTS_SPEAKING |
| `BotStartedSpeakingFrame` | IDLE → TTS_SPEAKING; LLM_PROCESSING → BOTH |
| `BotStoppedSpeakingFrame` | TTS_SPEAKING → IDLE; BOTH → LLM_PROCESSING |

### Frames that Pass Through Unchanged

- `StartFrame`
- `EndFrame`
- `InterruptionFrame`
- `CancelFrame`
- All other frames not listed above

---

## Interruption Logic

### Step-by-Step Flow

```
1. TranscriptionFrame arrives
2. Is state == IDLE?
   YES → Process normally, state = LLM_PROCESSING
   NO  → Continue to step 3
3. Push InterruptionFrame upstream (cancels LLM/TTS)
4. Wait for interruption to complete
5. Buffer the new transcription
6. Flush buffered transcription immediately
   → state = LLM_PROCESSING
   → Push frame downstream
```

### Key Code Path

```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    # 1. If interruption in progress, buffer transcriptions
    if self._interruption_in_progress and isinstance(frame, TranscriptionFrame):
        self._buffered_transcription = frame
        return  # Don't push, wait for interruption to finish

    # 2. Track LLM/TTS state changes
    if isinstance(frame, LLMFullResponseStartFrame):
        self._state = ResponseState.LLM_PROCESSING
    if isinstance(frame, BotStartedSpeakingFrame):
        self._state = ResponseState.BOTH
    # ... etc

    # 3. Handle transcription with interruption logic
    if isinstance(frame, TranscriptionFrame):
        if self._state != ResponseState.IDLE:
            # Active response → interrupt!
            self._interruption_in_progress = True
            await self.push_interruption_task_frame_and_wait()

            # Buffer and flush immediately
            self._buffered_transcription = frame
            self._interruption_in_progress = False
            self._state = ResponseState.IDLE
            await self._flush_buffered_transcription(direction)
            return

        # No active response
        self._state = ResponseState.LLM_PROCESSING
        await self.push_frame(frame, direction)
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

| Scenario | State Before | Action | Result |
|----------|--------------|--------|--------|
| Normal flow | IDLE | Process normally | Single response |
| LLM processing, new STT | `LLM_PROCESSING` | Cancel LLM, process new | New only |
| TTS speaking, new STT | `TTS_SPEAKING` | Stop TTS, process new | New only |
| Both LLM+TTS, new STT | `BOTH` | Cancel both, process new | New only |
| Quick double STT | `LLM_PROCESSING` | Buffer 2nd, flush after 1st | Latest only |

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
# Normal flow:
ResponseGate: Processing transcription (id=..., length=...)
ResponseGate: LLM_PROCESSING
ResponseGate: BOTH
ResponseGate: TTS_SPEAKING
ResponseGate: IDLE

# Interruption:
ResponseGate: Interrupting state=BOTH for new transcription (id=..., length=...)
ResponseGate: Processing transcription (id=..., length=...)
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
    def __init__(self, **kwargs):
        self._state = ResponseState.IDLE
        self._buffered_transcription = None
        self._interruption_in_progress = False

    async def _handle_transcription_frame(
        self, frame: TranscriptionFrame, direction: FrameDirection
    ):
        """Process a transcription frame (buffered or fresh)."""
        ...

    async def _flush_buffered_transcription(self, direction: FrameDirection):
        """Flush any buffered transcription immediately."""
        ...
```

### Methods

| Method | Purpose |
|--------|---------|
| `process_frame()` | Main entry point, handles all frame types |
| `_handle_transcription_frame()` | Unified handler for transcription frames |
| `_flush_buffered_transcription()` | Process buffered frame after interruption |

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

Buffer should flush immediately after interruption. If not:
1. Check `_flush_buffered_transcription()` is called
2. Verify no exception in the flush path

---

## Files

| File | Purpose |
|------|---------|
| `app/ai/voice/agents/breeze_buddy/processors/response_gate.py` | Main processor implementation |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` | Pipeline integration |
| `app/ai/voice/agents/breeze_buddy/template/types.py` | `InterruptionMode` and `InterruptionConfig` definitions |
| `docs/aggregation-timeout.md` | Related: aggregation timeout explanation |
