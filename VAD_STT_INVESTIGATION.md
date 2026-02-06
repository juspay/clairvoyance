# VAD and STT Investigation - Breeze Buddy

**Investigation Date:** 2026-02-06
**Issue:** Background speech being transcribed but not sent to LLM; STT accumulation without LLM delivery

---

## Executive Summary

**Root Cause Identified:** The VAD (Voice Activity Detection) does NOT filter audio frames before they reach STT. Instead, VAD only emits events (speech started/stopped) while ALL audio continues to be sent to Soniox for transcription.

When background speech has low confidence or low volume:
- VAD doesn't emit speech events (because thresholds aren't met)
- BUT audio is still sent to Soniox STT
- Soniox transcribes the audio
- Soniox waits for a finalize signal that never comes (because VAD never emitted a stop event)
- **Transcriptions accumulate indefinitely in Soniox's buffer**

---

## Current Architecture Flow

```
┌─────────────────┐
│  Microphone/    │
│  Audio Input    │
└────────┬────────┘
         │ (All audio frames)
         ▼
┌─────────────────────────────────────┐
│      Transport Layer                │
│  - Daily (16kHz) or                 │
│  - Telephony (8kHz)                 │
│                                     │
│  Contains: SileroVADAnalyzer        │
│  (DEPRECATED pattern)               │
└────┬───────────────────┬────────────┘
     │                   │
     │ All Audio         │ VAD Events Only
     │ Frames            │ (if thresholds met)
     ▼                   ▼
┌──────────────┐    ┌─────────────────────────┐
│ Soniox STT   │    │ VADUserStartedSpeaking  │
│              │    │ VADUserStoppedSpeaking  │
│ vad_force_   │◄───│                         │
│ turn_        │    │ Only emitted if:        │
│ endpoint:    │    │ - confidence >= 0.5/0.9 │
│ true         │    │ - min_volume >= 0.4/0.75│
│              │    └─────────────────────────┘
│ Transcribes  │
│ EVERYTHING   │
│ it receives  │
│              │
│ Buffers      │
│ transcripts  │
│ until        │
│ finalize/    │
│ endpoint     │
└──────┬───────┘
       │ TranscriptionFrame
       │ (only when finalized)
       ▼
┌────────────────────┐
│ context_aggregator │
│ .user()            │
└────────┬───────────┘
         │ LLMUserFrame
         ▼
┌────────────────┐
│  Azure LLM     │
│  (GPT-4)       │
└────────────────┘
```

---

## Detailed Findings

### 1. VAD Does NOT Filter Audio Frames

**Location:** `/tmp/pipecat/src/pipecat/audio/vad/vad_controller.py:112-124`

```python
async def _handle_audio(self, frame: InputAudioRawFrame):
    """Process an audio chunk and emit speech events as needed."""
    self._vad_state = await self._handle_vad(frame.audio, self._vad_state)

    if self._vad_state == VADState.SPEAKING:
        await self._call_event_handler("on_speech_activity")
```

**Key Insight:** The VAD controller:
- Analyzes audio to determine speech state
- **Emits events only** (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
- **Does NOT drop or filter audio frames**
- All audio continues downstream to STT

### 2. VAD State Transitions

**Location:** `/tmp/pipecat/src/pipecat/audio/vad/vad_analyzer.py:207-244`

```python
speaking = confidence >= self._params.confidence and volume >= self._params.min_volume

if speaking:
    match self._vad_state:
        case VADState.QUIET:
            self._vad_state = VADState.STARTING
        case VADState.STARTING:
            self._vad_starting_count += 1
        # ...
```

**Speech Detection Criteria (ALL must be met):**
1. `confidence >= params.confidence` (Silero model confidence score)
2. `volume >= params.min_volume` (audio volume threshold)
3. Duration >= `start_secs` (sustained for minimum duration)

**Breeze Buddy VAD Configuration:**

| Mode | confidence | start_secs | stop_secs | min_volume | Notes |
|------|-----------|------------|-----------|------------|-------|
| **Daily** (web/mobile) | **0.9** | 0.25s | 0.95s | **0.75** | Very strict |
| **Telephony** (phone) | 0.5 | 0.1s | 0.3s | 0.4 | More lenient |

**Source:** `app/core/config/dynamic.py:205-224` (Daily), `app/core/config/static.py:354-368` (Telephony)

### 3. Soniox STT Buffering Behavior

**Location:** `/tmp/pipecat/src/pipecat/services/soniox/stt.py:250-254, 308-310, 374-390`

#### Finalize Message Mechanism

```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    await super().process_frame(frame, direction)

    if isinstance(frame, VADUserStoppedSpeakingFrame) and self._vad_force_turn_endpoint:
        # Send finalize message to Soniox so we get the final tokens asap.
        if self._websocket and self._websocket.state is State.OPEN:
            await self._websocket.send(FINALIZE_MESSAGE)
```

**Configuration:**
- `vad_force_turn_endpoint=true` (Breeze Buddy) → External VAD controls turn endpoints
- `vad_force_turn_endpoint=false` (Automatic) → Soniox intelligent endpoint detection

```python
# If vad_force_turn_endpoint is not enabled, we need to enable endpoint detection.
# Either one or the other is required.
enable_endpoint_detection = not self._vad_force_turn_endpoint
```

#### Transcription Buffering

```python
async def _receive_messages(self):
    # Transcription frame will be only sent after we get the "endpoint" event.
    self._final_transcription_buffer = []

    async def send_endpoint_transcript():
        if self._final_transcription_buffer:
            text = "".join(map(lambda token: token["text"], self._final_transcription_buffer))
            await self.push_frame(
                TranscriptionFrame(
                    text=text,
                    user_id=self._user_id,
                    timestamp=time_now_iso8601(),
                    result=self._final_transcription_buffer,
                    finalized=True,
                )
            )
            self._final_transcription_buffer = []
```

**Key Behavior:**
- Soniox accumulates transcription tokens in `_final_transcription_buffer`
- **TranscriptionFrame is ONLY sent when an endpoint is detected**
- Endpoints come from:
  - External VAD: `VADUserStoppedSpeakingFrame` → finalize message
  - Internal: Soniox's intelligent endpoint detection
- **If neither occurs, transcriptions accumulate indefinitely**

---

## Problem Scenarios

### Scenario 1: Background Speech (Low Confidence/Volume)

```
Timeline:
T0: Background person speaks (low volume/confidence)
    ├─ Transport: Sends audio to Soniox ✓
    ├─ VAD: confidence=0.3 < 0.9 → No VADUserStartedSpeaking ✗
    └─ Soniox: Receives audio, starts transcribing

T1: Background person continues
    ├─ Transport: Continues sending audio ✓
    ├─ VAD: Still below threshold → No events ✗
    └─ Soniox: Adds tokens to buffer ("hi there")

T2: Background person stops
    ├─ Transport: Audio continues (ambient noise) ✓
    ├─ VAD: No speech detected → No VADUserStoppedSpeaking ✗
    └─ Soniox: Buffer never finalized → No TranscriptionFrame ✗

Result: "hi there" stuck in Soniox buffer forever 🔴
```

### Scenario 2: Intermittent Low-Confidence Speech

```
Timeline:
T0: User starts speaking clearly
    ├─ VAD: Emits VADUserStartedSpeaking ✓
    ├─ Soniox: Starts buffering

T1: User speaks softly (volume < min_volume)
    ├─ VAD: Still in STOPPING state (waiting for stop_secs)
    ├─ Soniox: Continues transcribing soft speech

T2: User returns to normal volume
    ├─ VAD: Transitions back to SPEAKING
    ├─ Soniox: Continues buffering

T3: Timeout occurs before VAD detects stop
    ├─ VAD: Eventually emits VADUserStoppedSpeaking ✓
    └─ Soniox: Finalizes and sends all buffered text ✓

Result: Works, but with delay ⚠️
```

### Scenario 3: Multiple Speakers

```
Timeline:
T0: Primary user speaks
    ├─ VAD: VADUserStartedSpeaking ✓
    ├─ Soniox: Buffering "I would like to order"

T1: Background person interjects
    ├─ Audio: Mixed audio (both speakers)
    ├─ VAD: Confidence fluctuates, but stays > threshold
    ├─ Soniox: Transcribes both voices: "I would like background noise order shoes"

T2: Primary user finishes
    ├─ VAD: VADUserStoppedSpeaking ✓
    └─ Soniox: Sends contaminated transcription ⚠️

Result: Transcription includes background speech 🔴
```

---

## Root Cause Analysis

### Why VAD is After Transport, Not Before STT

**Historical Context:**
- Pipecat v0.0.101+ deprecated VAD in transport
- New pattern: VAD in `LLMUserAggregator` via `vad_analyzer` parameter
- **Breeze Buddy uses the OLD pattern** (VAD in transport params)

**Current Implementation:**
```python
# app/ai/voice/agents/breeze_buddy/agent/transport.py:16-34
def get_transport_params(vad_analyzer: Optional[SileroVADAnalyzer], ...):
    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,  # ← Deprecated pattern
        ),
        # ...
    }

# app/ai/voice/agents/breeze_buddy/agent/pipeline.py:131-137
context_aggregator = llm.create_context_aggregator(
    context,
    user_params=LLMUserAggregatorParams(
        aggregation_timeout=await BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT(),
        enable_emulated_vad_interruptions=ENABLE_BREEZE_BUDDY_USER_INTERRUPTION,
        # vad_analyzer parameter NOT set ← Missing in Breeze Buddy
    ),
)
```

**Why This Matters:**
- Transport VAD only generates events
- It does NOT filter audio frames
- Even with new `LLMUserAggregator.vad_analyzer`, VAD still doesn't filter audio to STT
- **VAD was never designed to gate audio to STT** - it's for turn detection and interruption handling

### Architectural Misunderstanding

**Common Expectation (INCORRECT):**
```
Audio → VAD (filter) → Only "clean speech" → STT → LLM
```

**Actual Reality:**
```
Audio ──┬→ VAD (events only) → VADUserStarted/Stopped
        │
        └→ STT (all audio) → TranscriptionFrame (when finalized)
```

---

## Evidence from Code

### 1. VAD Analyzer Never Drops Frames

**File:** `/tmp/pipecat/src/pipecat/audio/vad/vad_controller.py:90-105`

```python
async def process_frame(self, frame: Frame):
    """Process a frame and handle VAD-related events."""
    if isinstance(frame, StartFrame):
        await self._start(frame)
    elif isinstance(frame, InputAudioRawFrame):
        await self._handle_audio(frame)  # Analyzes but doesn't block/drop
    elif isinstance(frame, VADParamsUpdateFrame):
        self._vad_analyzer.set_params(frame.params)
```

**No return statement, no frame dropping** - VAD is purely observational.

### 2. Soniox Receives All Audio

**File:** `/tmp/pipecat/src/pipecat/services/soniox/stt.py:218-232`

```python
async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
    """Send audio data to Soniox STT Service."""
    await self.start_processing_metrics()
    if self._websocket and self._websocket.state is State.OPEN:
        await self._websocket.send(audio)  # ← Every audio chunk sent
    await self.stop_processing_metrics()
    yield None
```

**No VAD check before sending to Soniox** - all audio is processed.

### 3. Transcription Gating Happens at Finalization

**File:** `/tmp/pipecat/src/pipecat/services/soniox/stt.py:374-390, 409-418`

```python
async def _receive_messages(self):
    # Transcription frame will be only sent after we get the "endpoint" event.
    self._final_transcription_buffer = []

    # ...

    for token in tokens:
        if token["is_final"]:
            if is_end_token(token):
                # Found an endpoint, tokens until here will be sent as transcript
                await send_endpoint_transcript()  # ← Only here!
            else:
                self._final_transcription_buffer.append(token)
```

**Transcriptions are buffered until endpoint** - low-confidence speech is transcribed but never sent.

---

## Recommendations

### Option 1: Enable Soniox Intelligent Endpoint Detection (RECOMMENDED)

**Change:**
```python
# app/core/config/static.py
BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT = False  # Currently True
```

**Benefits:**
- Soniox's ML model detects natural turn boundaries
- No dependency on external VAD thresholds
- Automatically handles mixed-confidence scenarios
- Reduces false accumulations

**Trade-offs:**
- May have slightly different turn boundary detection
- Less control over turn timing
- Need to test with actual call scenarios

**Files to Update:**
1. `app/core/config/static.py:464`
2. Test with Daily and Telephony modes

---

### Option 2: Add Audio Gating in Pipeline (COMPLEX)

Create a custom frame processor that filters audio based on VAD state.

**Implementation:**

```python
# app/ai/voice/agents/breeze_buddy/processors/vad_audio_gate.py

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


class VADAudioGate(FrameProcessor):
    """Filters audio frames based on VAD state.

    Only allows InputAudioRawFrame through when user is speaking.
    Drops audio when VAD hasn't detected speech.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._user_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Track VAD state
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_speaking = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_speaking = False
            await self.push_frame(frame, direction)
        # Filter audio frames
        elif isinstance(frame, InputAudioRawFrame):
            if self._user_speaking:
                await self.push_frame(frame, direction)
            # else: drop frame silently
        else:
            # Pass all other frames through
            await self.push_frame(frame, direction)
```

**Pipeline Integration:**

```python
# app/ai/voice/agents/breeze_buddy/agent/pipeline.py

from app.ai.voice.agents.breeze_buddy.processors.vad_audio_gate import VADAudioGate

async def build_pipeline(...):
    # ...

    vad_gate = VADAudioGate()  # Create gate

    pipeline_parts = [
        transport.input(),
        vad_gate,           # ← Insert BEFORE STT
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]
    # ...
```

**Benefits:**
- Complete control over what audio reaches STT
- Eliminates background speech transcription
- Reduces Soniox API usage

**Trade-offs:**
- May cut off speech beginnings if VAD is slow to detect
- Could miss soft-spoken utterances
- Adds complexity to pipeline
- Need careful tuning of VAD parameters

---

### Option 3: Migrate to New VAD Pattern (FUTURE)

**Recommended for Long-Term:**

Move VAD from transport to LLMUserAggregator (Pipecat's new pattern).

```python
# app/ai/voice/agents/breeze_buddy/agent/pipeline.py

async def build_pipeline(transport, stt, llm, tts, vad_analyzer):
    context = OpenAILLMContext()
    context_aggregator = llm.create_context_aggregator(
        context,
        user_params=LLMUserAggregatorParams(
            aggregation_timeout=await BREEZE_BUDDY_LLM_AGGREGATION_TIMEOUT(),
            enable_emulated_vad_interruptions=ENABLE_BREEZE_BUDDY_USER_INTERRUPTION,
            vad_analyzer=vad_analyzer,  # ← Move VAD here
        ),
    )
    # ...
```

**Transport Params (remove VAD):**

```python
# app/ai/voice/agents/breeze_buddy/agent/transport.py

def get_transport_params(audio_out_mixer: Optional[SoundfileMixer] = None):
    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # vad_analyzer=vad_analyzer,  # ← Remove this
        ),
        # ...
    }
```

**Benefits:**
- Follows Pipecat best practices
- Better integration with turn detection
- Cleaner separation of concerns

**Trade-offs:**
- Requires testing entire pipeline
- May affect existing VAD-dependent features
- Need to verify backward compatibility

---

### Option 4: Tune VAD Parameters (TACTICAL)

**For Daily Mode (currently very strict):**

```python
# app/core/config/dynamic.py or Redis config

BB_DAILY_VAD_CONFIDENCE: 0.7  # Currently 0.9 (too strict)
BB_DAILY_VAD_MIN_VOLUME: 0.5  # Currently 0.75 (too strict)
```

**Benefits:**
- Quick fix without code changes
- Can be adjusted per-environment
- A/B testable

**Trade-offs:**
- More false positives (background noise detected as speech)
- May trigger VAD for TV, music, other household sounds
- Doesn't solve the fundamental issue

---

## Testing Plan

### 1. Verify Current Behavior

**Test Setup:**
- Enable detailed logging for VAD and Soniox
- Monitor Soniox transcription buffer

**Logging to Add:**

```python
# app/ai/voice/stt/soniox.py (modify build_soniox_stt)

logger.info(
    "Soniox configuration: vad_force_turn_endpoint=%s, model=%s",
    config.vad_force_turn_endpoint,
    config.model,
)

# In pipecat fork or local patch:
# /tmp/pipecat/src/pipecat/services/soniox/stt.py

# After line 416 (in _receive_messages):
if self._final_transcription_buffer:
    logger.debug(
        f"Soniox buffer size: {len(self._final_transcription_buffer)} tokens, "
        f"text: {final_text[:50]}..."
    )
```

**Test Scenarios:**
1. Normal user speech (should work)
2. Background person speaks during silence (should accumulate)
3. Mixed audio (user + background simultaneously)

### 2. Test Option 1 (Soniox Endpoint Detection)

**Configuration Change:**
```bash
# In .env or config
BREEZE_BUDDY_SONIOX_VAD_FORCE_TURN_ENDPOINT=false
```

**Verify:**
- Background speech is still transcribed
- BUT endpoints are detected by Soniox
- Transcriptions are sent even without VAD events
- Check for false turn boundaries

### 3. Test Option 2 (Audio Gating)

**Implementation:**
1. Add VADAudioGate processor
2. Insert in pipeline before STT
3. Monitor dropped frames

**Metrics:**
- Audio frames sent to Soniox (should decrease)
- Transcription accuracy (ensure speech isn't cut off)
- Background speech transcription (should be eliminated)

---

## Metrics to Monitor

### Before Fix:
- Soniox buffer size over time
- Transcriptions sent vs. transcriptions buffered
- VAD events emitted vs. audio duration

### After Fix:
- Transcription latency
- False positive rate (background detected as speech)
- False negative rate (speech not detected)
- API usage (Soniox transcription requests)

---

## Configuration Reference

### Current VAD Settings

| Setting | Daily Mode | Telephony Mode | Impact |
|---------|-----------|----------------|--------|
| `confidence` | **0.9** | 0.5 | Speech detection threshold |
| `start_secs` | 0.25 | 0.1 | Min duration to trigger start |
| `stop_secs` | 0.95 | 0.3 | Silence duration to trigger stop |
| `min_volume` | **0.75** | 0.4 | Audio volume threshold |
| Sample Rate | 16000 Hz | 8000 Hz | Audio quality |

**Files:**
- Daily: `app/core/config/dynamic.py:205-224`
- Telephony: `app/core/config/static.py:354-368`

### Current Soniox Settings

| Setting | Breeze Buddy | Automatic | Impact |
|---------|-------------|-----------|--------|
| `model` | stt-rt-v3 | stt-rt-preview | Transcription model |
| `vad_force_turn_endpoint` | **true** | false | Turn detection method |
| `language_hints` | "en,hi" | "en" | Language prioritization |
| `enable_non_final_tokens` | true | false | Interim results |

**Files:**
- `app/core/config/static.py:446-464`
- `app/ai/voice/stt/soniox.py:108-175`

---

## Conclusion

**You are correct** - VAD should ideally come before STT to filter audio. However, in the current Pipecat architecture:

1. **VAD is event-based, not filter-based** - It emits events but doesn't gate audio
2. **All audio reaches STT** - This is by design in Pipecat
3. **Filtering happens at transcription finalization** - Not at audio ingestion

**Immediate Fix:** Enable Soniox intelligent endpoint detection (`vad_force_turn_endpoint=false`)

**Long-Term Fix:** Add explicit audio gating processor or migrate to new Pipecat VAD pattern

---

## Next Steps

1. ✅ Test Option 1 (Soniox endpoint detection) in dev environment
2. Monitor Soniox buffer accumulation with detailed logging
3. If Option 1 insufficient, implement Option 2 (VADAudioGate)
4. Consider Option 3 (VAD migration) for next major refactor
5. Document final solution and update architecture diagrams

---

**Investigation completed by:** Claude Code
**Repository:** juspay/clairvoyance
**Branch:** claude/soniox-vad-filtering-i4hn7
