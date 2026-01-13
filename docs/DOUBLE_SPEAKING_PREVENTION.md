# Double Speaking Prevention - Response Gate Strategy

## Overview

This documentation covers the "response gate" architecture used to prevent "double speaking" in voice AI pipelines when using **0-second aggregation time**.

### File Structure

```
processors/
├── response_gate.py     # ResponseGateState, ResponseGateTracker
└── tts_interrupter.py   # AudioInterruptionProcessor
```

---

## The Problem: Double Speaking

When aggregation time is set to 0 seconds, a race condition occurs:

```
Timeline (without gate):
─────────────────────────────────────────────────────────────────────────────
0ms     User: "Hello"
        └─► STT → TranscriptionFrame("Hello") → LLM starts generating

100ms   LLM: "Hi! How can I help..." (still generating)

150ms   User: "What is Java?" (speaks again quickly)
        └─► STT → TranscriptionFrame("What is Java?") → NEW LLM request!

200ms   TTS starts playing: "Hi! How can I help..."
        LLM #2 starts: "Java is a programming language..."

500ms   TTS plays SECOND response immediately after first!
─────────────────────────────────────────────────────────────────────────────

Result: Bot speaks TWICE back-to-back = BAD USER EXPERIENCE
```

---

## The Solution: Latest Wins Strategy

Instead of blocking transcriptions, we use a **"latest wins"** approach:
- **ALL transcriptions are allowed through** to the LLM
- When a **new transcription arrives while the bot is responding**, the old response is interrupted
- This ensures the user's most recent input is always honored

### Pipeline Layout

```
┌─────────┐    ┌─────┐    ┌─────────────────┐    ┌──────────────┐    ┌─────┐    ┌─────┐    ┌─────────────────┐    ┌────────┐
│  Input  │───►│ STT │───►│ ResponseGate    │───►│  Aggregator  │───►│ LLM │───►│ TTS │───►│ AudioInterruption│───►│ Output │
│         │    │     │    │    Tracker      │    │              │    │     │     │     │ │   Processor     │    │        │
└─────────┘    └─────┘    └─────────────────┘    └──────────────┘    └─────┘    └─────┘    └─────────────────┘    └────────┘
                               ▲                                                            ▲
                               │                                                            │
                         Response gate:                                               Interrupt old
                         allow all, interrupt                                     TTS on new LLM
                         on new user input                                       response start
```

---

## Component 1: ResponseGateTracker (response_gate.py)

**Position:** BEFORE the LLM (upstream in the pipeline)

**Strategy:** Allow ALL transcriptions through, but interrupt if response is active.

**Behavior:**
- On `LLMFullResponseStartFrame` → Set `response_pending = True`
- On `BotStartedSpeakingFrame` → Set `tts_playing = True`
- On `BotStoppedSpeakingFrame` → Reset both flags
- On `TranscriptionFrame`: If response active, push `InterruptionFrame` downstream, then allow transcription through

### How Interruption Works

When a new transcription arrives while the bot is responding:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ New Transcription arrives while tts_playing=True or response_pending=True  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │ Push InterruptionFrame        │
                    │ DOWNSTREAM                    │
                    │ (triggers pipecat's           │
                    │  interruption mechanism)      │
                    └───────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │ Reset state:                  │
                    │ - tts_playing = False         │
                    │ - response_pending = False    │
                    └───────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │ Allow new Transcription       │
                    │ through to LLM                │
                    └───────────────────────────────┘
```

---

## Component 2: TTSInterruptionProcessor (tts_interrupter.py)

**Position:** AFTER TTS (downstream in the pipeline)

**Strategy:** Interrupt if a NEW LLM response starts while previous is still active.

**Behavior:**
- On `BotStartedSpeakingFrame` → Set `tts_playing = True`
- On `BotStoppedSpeakingFrame` → Reset both flags
- On `LLMFullResponseStartFrame`: If TTS playing or response pending, push `BotStoppedSpeakingFrame` UPSTREAM to interrupt

### Why Two Interruption Mechanisms?

| Trigger | Processor | Frame Sent | Direction | Purpose |
|---------|-----------|------------|-----------|---------|
| New user transcription | ResponseGateTracker | `InterruptionFrame` | DOWNSTREAM | Stop TTS, clear queues |
| New LLM response | AudioInterruptionProcessor | `BotStoppedSpeakingFrame` | UPSTREAM | Cancel queued audio, signal LLM cancel |

Both are needed because:
1. User might interrupt while TTS is playing
2. LLM might generate a new response while old one hasn't finished TTS

---

## Shared State: ResponseGateState

Both processors share an instance of `ResponseGateState` to coordinate.

**Each Agent instance must create its own `ResponseGateState` to avoid cross-talk between concurrent agents.**

```python
class ResponseGateState:
    tts_playing: bool           # True when TTS audio is playing
    response_pending: bool      # True from LLMFullResponseStart until BotStoppedSpeaking

    def reset(self):
        """Reset all state when bot stops responding."""
```

### State Transitions

```
                    ┌─────────────────────────────────────┐
                    │         IDLE STATE                  │
                    │  tts_playing = False                │
                    │  response_pending = False           │
                    └─────────────────────────────────────┘
                                    │
                                    │ TranscriptionFrame
                                    ▼
                    ┌─────────────────────────────────────┐
                    │      LLM GENERATING                 │
                    │  response_pending = True            │
                    │  (gate stays open - latest wins!)   │
                    └─────────────────────────────────────┘
                                    │
                                    │ BotStartedSpeakingFrame
                                    ▼
                    ┌─────────────────────────────────────┐
                    │      BOT SPEAKING                   │
                    │  tts_playing = True                 │
                    │  response_pending = True            │
                    └─────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               │               ▼
        ┌───────────────────┐      │      ┌───────────────────┐
        │ BotStoppedSpeaking│      │      │ New Transcription │
        │ → Back to IDLE    │      │      │ → Interruption +  │
        └───────────────────┘      │      │   Allow through   │
                                   │      └───────────────────┘
                                   └──────────────────────────────►
```

---

## Comparison: Old vs New Strategy

| Aspect | Old (Blocking) | New (Response Gate) |
|--------|----------------|---------------------|
| Transcriptions | Blocked while responding | All allowed through |
| User Experience | Delayed response | Immediate response |
| Intent Honored | First request wins | Latest request wins |
| Complexity | Complex gating logic | Simple interruption |

---

## Usage

### In Pipeline Configuration ([agent.py](../app/ai/voice/agents/breeze_buddy/agent.py))

```python
from app.ai.voice.agents.breeze_buddy.processors.response_gate import (
    ResponseGateState,
    ResponseGateTracker,
)
from app.ai.voice.agents.breeze_buddy.processors.tts_interrupter import (
    AudioInterruptionProcessor,
)

# Create shared state for this agent instance (prevents cross-talk)
state = ResponseGateState()

# Create processors with shared state
response_gate = ResponseGateTracker(state=state)
audio_interrupter = AudioInterruptionProcessor(state=state)

# Build pipeline
pipeline = Pipeline([
    transport.input(),
    stt,
    response_gate,          # ← GATE 1: Before aggregator/LLM
    context_aggregator.user(),
    llm,
    tts,
    audio_interrupter,      # ← GATE 2: After TTS
    transport.output(),
    context_aggregator.assistant(),
])
```

---

## Debug Logging

The module logs all state transitions at DEBUG level:

```
ResponseGate: LLM response started (response_pending=True)
ResponseGate: TTS started playing
ResponseGate: New transcription while response active - INTERRUPTING
ResponseGate: Allowing transcription: 'What is Java?'
AudioInterruption: New LLM response started while previous active - INTERRUPTING
AudioInterruption: Bot stopped speaking, response complete
```

---

## Edge Cases Handled

### 1. User Interrupts Mid-TTS
- New `TranscriptionFrame` arrives while `tts_playing=True`
- `InterruptionFrame` is pushed downstream to stop TTS
- New transcription flows through to LLM

### 2. LLM Starts New Response Before TTS Finishes
- `LLMFullResponseStartFrame` arrives while `response_pending=True`
- `AudioInterruptionProcessor` pushes `BotStoppedSpeakingFrame` upstream
- Old response is cancelled, new one proceeds

### 3. Rapid-Fire User Input
- Multiple transcriptions arrive quickly
- Each new one interrupts the previous
- Only the FINAL transcription's response plays

### 4. LLM Finishes But TTS Hasn't Started
- `response_pending=True` but `tts_playing=False`
- New transcription still triggers interruption
- Old LLM response is discarded before TTS begins

---

## Why "Response Gate" Instead of Blocking?

### Problems with Blocking Strategy

```
Timeline (blocking approach):
─────────────────────────────────────────────────────────────────────────────
0ms     User: "Hello"
        └─► Gate: OPEN → LLM starts generating

100ms   User: "Actually wait..."
        └─► Gate: CLOSED → Blocked! ❌

150ms   LLM: "Hi! How can I..." (finishes)

200ms   User: "What is Java?"
        └─► Gate: OPEN → LLM #2 starts

300ms   TTS: "Hi! How can I help..." (plays old response) ❌

Result: Old response still plays, user intent ignored!
─────────────────────────────────────────────────────────────────────────────
```

### Why Response Gate Works Better

```
Timeline (response gate approach):
─────────────────────────────────────────────────────────────────────────────
0ms     User: "Hello"
        └─► Gate: OPEN → LLM #1 starts

100ms   User: "Actually wait..."
        └─► Gate: OPEN → Interrupts LLM #1 → LLM #2 starts

150ms   User: "What is Java?"
        └─► Gate: OPEN → Interrupts LLM #2 → LLM #3 starts

300ms   TTS: "Java is a programming language..." ✅
─────────────────────────────────────────────────────────────────────────────

Result: Only the latest response plays!
```

---

## Architecture Decisions

### Why Two Processors in Separate Files?

1. **Separation of Concerns**
   - `response_gate.py`: Interrupts on new user input (upstream)
   - `tts_interrupter.py`: Interrupts on new LLM response (downstream)

2. **Pipeline Position Matters**
   - Gate 1 must be BEFORE LLM to intercept transcriptions
   - Gate 2 must be AFTER TTS to track/interrupt audio

3. **Different Interruption Mechanisms**
   - `InterruptionFrame` (downstream): Triggers pipecat's built-in interruption
   - `BotStoppedSpeakingFrame` (upstream): Signals TTS to stop, LLM to cancel

### Why Shared State?

- Both processors need coordinated state
- `ResponseGateState` tracks `tts_playing` and `response_pending`
- Enables coordination without complex message passing

### Why Instance-Based State?

**Problem with module-level state:**
- Multiple concurrent Agent instances would share the same state
- Agent A's state changes would affect Agent B → **cross-talk**

**Solution:**
- Each Agent creates its own `ResponseGateState` instance
- Both processors for that agent share the same instance
- No cross-talk between concurrent agents

---

## Troubleshooting

### Bot Still Double-Speaking

1. Check pipeline order - `ResponseGateTracker` must be BEFORE `context_aggregator.user()`
2. Check logs for "INTERRUPTING" when new transcription arrives
3. Verify `AudioInterruptionProcessor` is AFTER `tts`

### Interruption Not Working

1. Check if `tts_playing` and `response_pending` are being set correctly
2. Look for missing `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` events
3. Verify TTS is emitting proper start/stop frames

### State Not Resetting

1. Check for `BotStoppedSpeakingFrame` in logs
2. Verify `state.reset()` or manual reset is being called
3. Look for exceptions in frame processing

---

## Comparison with Pipecat's Built-in Interruption

| Feature | Pipecat Built-in | Response Gate Processors |
|---------|------------------|--------------------------|
| Cancel LLM when user speaks | ✅ | ✅ (via InterruptionFrame) |
| Stop TTS playback | ✅ | ✅ (via InterruptionFrame) |
| Prevent 0-second aggregation issues | ❌ | ✅ |
| "Latest wins" behavior | ❌ | ✅ |
| Works with context aggregators | ❌ | ✅ |

The Response Gate processors **complement** Pipecat's built-in interruption by handling the specific race conditions that occur with 0-second aggregation time.
