# VAD System Investigation Report - BreezeBuddy Agent

## Executive Summary

The intermittent failure of VAD (Voice Activity Detection) to trigger "User started speaking" events is caused by a combination of **borderline volume thresholds**, **aggressive exponential smoothing**, and **the AND-gate requirement** between confidence and volume. Additionally, a **code-level bug** in how VAD parameters are modified (bypassing `set_params()`) prevents `start_secs`/`stop_secs` changes from taking effect during runtime.

---

## Log Analysis: The Smoking Gun

From the production logs:

```
18:37:11 | Bot stopped speaking
18:37:11 | ResponseGate: LLM_PROCESSING (TTS stopped)
18:37:15 | INTERIM TRANSCRIPTION: ' एड' at 65.40s          ← Soniox recognizes speech
18:37:15 | INTERIM TRANSCRIPTION: ' एडजस्टमेंट' at 65.74s   ← More speech detected by STT
...many more interim transcriptions...
18:37:19 | User started speaking                            ← VAD finally fires (4 sec late!)
18:37:19 | InterruptionTaskFrame#5
```

**Key observation**: Soniox STT produced interim transcriptions starting at 18:37:15, proving it was receiving audio and recognizing speech. But Silero VAD didn't fire "User started speaking" until 18:37:19 - a **~4 second delay**.

This happens because **STT receives audio independently of VAD** (audio passes through to STT regardless of VAD state), but the "User started speaking" event and interruption logic are entirely gated by VAD.

---

## How VAD Works (Deep Technical Analysis)

### Architecture

```
Audio Input (8kHz telephony)
    ↓
BaseInputTransport._audio_task_handler()
    ├── VAD: SileroVADAnalyzer.analyze_audio(audio_bytes)
    │    ├── voice_confidence(buffer)  → Silero ONNX model → [0.0, 1.0]
    │    ├── calculate_audio_volume()  → EBU R128 loudness → [0.0, 1.0]
    │    └── State machine: QUIET → STARTING → SPEAKING → STOPPING → QUIET
    │
    └── push_frame(audio) → STT → ... (audio flows regardless of VAD)
```

### The Detection Algorithm (vad_analyzer.py:190-244)

For each audio chunk (256 samples at 8kHz = 32ms per frame):

```python
speaking = confidence >= params.confidence AND volume >= params.min_volume
```

State transitions:
- **QUIET → STARTING**: First frame where `speaking=True`, counter starts at 1
- **STARTING → SPEAKING**: `_vad_starting_count >= _vad_start_frames` (consecutive frames)
- **STARTING → QUIET**: ANY frame where `speaking=False` **resets the counter to 0**
- **SPEAKING → STOPPING**: First frame where `speaking=False`
- **STOPPING → QUIET**: `_vad_stopping_count >= _vad_stop_frames`
- **STOPPING → SPEAKING**: ANY frame where `speaking=True` goes right back

### Your Production Config

| Parameter | Value | What it means |
|-----------|-------|---------------|
| confidence | 0.5 | Silero model output must be >= 0.5 |
| min_volume | 0.4 | EBU R128 normalized loudness must be >= 0.4 |
| start_secs | 0.1 | ~3 consecutive frames above both thresholds |
| stop_secs | 0.3 | ~9 consecutive frames below threshold |

Frame math at 8kHz:
- Frame size: 256 samples / 8000 Hz = **0.032 sec per frame**
- start_frames: round(0.1 / 0.032) = **3 consecutive frames** needed
- stop_frames: round(0.3 / 0.032) = **9 consecutive frames** needed

---

## Root Causes (Ranked by Impact)

### 1. CRITICAL: Exponential Volume Smoothing Creates a Ramp-Up Barrier

**File**: `pipecat/audio/vad/vad_analyzer.py:169-172`

```python
def _get_smoothed_volume(self, audio: bytes) -> float:
    volume = calculate_audio_volume(audio, self.sample_rate)
    return exp_smoothing(volume, self._prev_volume, self._smoothing_factor)
```

The smoothing factor is **hardcoded at 0.2** (`vad_analyzer.py:86`). This means:

```
new_smoothed = prev_smoothed + 0.2 * (actual_volume - prev_smoothed)
```

Starting from silence (prev_volume=0), even if the user speaks at a consistent volume of 0.5:

| Frame | Actual Volume | Smoothed Volume | Above 0.4? |
|-------|--------------|-----------------|-------------|
| 1 | 0.50 | 0.10 | No |
| 2 | 0.50 | 0.18 | No |
| 3 | 0.50 | 0.24 | No |
| 4 | 0.50 | 0.30 | No |
| 5 | 0.50 | 0.34 | No |
| 6 | 0.50 | 0.37 | No |
| 7 | 0.50 | 0.40 | **Barely** |
| 8 | 0.50 | 0.42 | Yes |

That's **8 frames × 32ms = 256ms** just for volume to ramp up from silence - before the 3-frame consecutive requirement even starts.

**But if the actual volume is closer to the threshold (e.g., 0.45 on telephony audio):**

| Frame | Actual Volume | Smoothed Volume | Above 0.4? |
|-------|--------------|-----------------|-------------|
| 1 | 0.45 | 0.09 | No |
| 5 | 0.45 | 0.28 | No |
| 10 | 0.45 | 0.37 | No |
| 12 | 0.45 | 0.39 | No |
| 13 | 0.45 | 0.40 | **Barely** |
| 14 | 0.45 | 0.41 | Yes |

That's **14 frames = 448ms** minimum, and if any frame dips (breath, pause between syllables), the smoothed volume drops and the start counter resets.

**For users with volumes hovering around 0.4-0.45, this can take seconds or never trigger at all.**

### 2. HIGH: EBU R128 Loudness Not Ideal for 8kHz Telephony

**File**: `pipecat/audio/utils.py:153-177`

```python
def calculate_audio_volume(audio: bytes, sample_rate: int) -> float:
    audio_np = np.frombuffer(audio, dtype=np.int16)
    audio_float = audio_np.astype(np.float64)
    block_size = audio_np.size / sample_rate
    meter = pyln.Meter(sample_rate, block_size=block_size)
    loudness = meter.integrated_loudness(audio_float)
    loudness = normalize_value(loudness, -20, 80)  # Maps [-20dB, 80dB] → [0.0, 1.0]
    return loudness
```

Problems:
- **EBU R128 is designed for broadcast audio**, not narrowband telephony
- At 8kHz, audio bandwidth is 0-4kHz (voice fundamentals only, no high harmonics)
- The K-weighting filter in R128 emphasizes 2-4kHz range, which is partially cut off at 8kHz
- **The normalization range [-20dB, 80dB] spans 100dB**, mapping to [0.0, 1.0]. Conversational speech on telephony likely maps to 0.3-0.5 in this scale - right at the threshold
- Volume on telephony varies wildly based on: phone model, carrier codec, network quality, ambient noise, speaker distance from mic

### 3. HIGH: AND-Gate Requirement with Consecutive Frames

The condition `speaking = confidence >= 0.5 AND volume >= 0.4` must be true for **3 consecutive frames** (start_secs=0.1).

On borderline audio, you might get patterns like:
```
Frame 1: conf=0.6, vol=0.42 → speaking=True  → STARTING (count=1)
Frame 2: conf=0.4, vol=0.45 → speaking=False → QUIET (count reset!)
Frame 3: conf=0.7, vol=0.38 → speaking=False → QUIET
Frame 4: conf=0.5, vol=0.41 → speaking=True  → STARTING (count=1)
Frame 5: conf=0.5, vol=0.39 → speaking=False → QUIET (count reset again!)
```

Both thresholds fluctuate independently. They need to BOTH exceed their limits simultaneously for 3 frames in a row. On borderline audio, this is probabilistically difficult.

### 4. MEDIUM: Silero Model Accuracy at 8kHz

While Silero natively supports 8kHz, it was primarily trained on 16kHz audio. At 8kHz:
- Less frequency information available for the model
- Confidence scores tend to be lower and more variable
- The model state resets every 5 seconds (`_MODEL_RESET_STATES_TIME = 5.0`), which can cause temporary accuracy drops right after reset

### 5. BUG: Direct Param Modification Bypasses set_params()

**Files**: `app/ai/voice/agents/breeze_buddy/template/vad.py`

All VAD param modification functions (`reset_vad_to_default`, `apply_node_vad_config`, `mute_vad`, `unmute_vad`) modify params directly:

```python
# This modifies the param value but does NOT recalculate frame counts
bot.vad_analyzer.params.start_secs = bot.default_vad_params.start_secs
bot.vad_analyzer.params.stop_secs = bot.default_vad_params.stop_secs
```

But `VADAnalyzer.set_params()` is what recalculates `_vad_start_frames` and `_vad_stop_frames`:

```python
def set_params(self, params: VADParams):
    self._params = params
    vad_frames_per_sec = self._vad_frames / self.sample_rate
    self._vad_start_frames = round(self._params.start_secs / vad_frames_per_sec)
    self._vad_stop_frames = round(self._params.stop_secs / vad_frames_per_sec)
    self._vad_starting_count = 0
    self._vad_stopping_count = 0
    self._vad_state = VADState.QUIET
```

**Impact**: Any runtime changes to `start_secs` or `stop_secs` (e.g., node-level VAD config with different stop_secs) are **silently ignored**. Only `confidence` and `min_volume` changes take effect because they're read directly in `_run_analyzer`.

This means if a node sets `stop_secs: 2.0` and then transitions back, the actual stop behavior doesn't change - it stays at whatever was calculated during initialization.

---

## Impact Analysis

When VAD fails to detect speech:

1. **No "User started speaking" event** → No interruption of bot speech
2. **No interruption means**: Bot keeps talking over the user, or the system waits until VAD eventually triggers
3. **Delayed transcription processing**: Even though Soniox produces interim transcriptions, the final transcription (which triggers LLM response) is delayed because `VADUserStoppedSpeakingFrame` (which triggers Soniox finalize) can't fire until VAD first detects speech
4. **Poor user experience**: User speaks, nothing happens for seconds, then suddenly the system catches up

---

## Recommendations

### Immediate Fixes (Config Changes)

#### 1. Lower min_volume to 0.15-0.25

```
Current:  min_volume = 0.4
Proposed: min_volume = 0.2
```

Rationale: On telephony at 8kHz, the EBU R128 normalized volume for normal speech is significantly lower than on direct microphone input. A value of 0.2 maps to approximately -20 + 0.2*100 = 0dB integrated loudness, which is a reasonable floor for telephony speech.

Risk: More false positives from background noise. Mitigated by the confidence threshold still requiring the Silero model to detect speech patterns.

#### 2. Lower confidence to 0.35-0.45

```
Current:  confidence = 0.5
Proposed: confidence = 0.4
```

Rationale: At 8kHz with telephony codecs, Silero produces lower confidence scores. Combined with the start_secs requirement for consecutive frames, a slightly lower threshold still provides good accuracy.

#### 3. Consider increasing start_secs slightly

```
Current:  start_secs = 0.1 (3 frames)
Proposed: start_secs = 0.15 (5 frames)
```

Rationale: If you lower both thresholds, you may want slightly more consecutive frames to avoid false triggers. This compensates for the looser thresholds while still being fast enough.

### Code Fixes

#### 4. Fix the set_params bug (HIGH PRIORITY)

In `app/ai/voice/agents/breeze_buddy/template/vad.py`, all functions that modify VAD params should call `set_params()` instead of direct attribute modification:

```python
# BEFORE (broken for start_secs/stop_secs):
bot.vad_analyzer.params.confidence = value
bot.vad_analyzer.params.start_secs = value
bot.vad_analyzer.params.stop_secs = value
bot.vad_analyzer.params.min_volume = value

# AFTER (correct):
new_params = VADParams(
    confidence=new_confidence,
    start_secs=new_start_secs,
    stop_secs=new_stop_secs,
    min_volume=new_min_volume,
)
bot.vad_analyzer.set_params(new_params)
```

Note: `set_params()` also resets `_vad_state` to QUIET, which may or may not be desired during active conversations. Consider whether you need a lighter-weight param update that recalculates frame counts without resetting state.

#### 5. Add diagnostic logging

Add volume and confidence logging to help diagnose future issues. Create a custom VAD analyzer wrapper:

```python
class DiagnosticVADAnalyzer(SileroVADAnalyzer):
    def _run_analyzer(self, buffer):
        # Log actual values every N frames for debugging
        confidence = self.voice_confidence(buffer[:self._vad_frames_num_bytes])
        volume = self._get_smoothed_volume(buffer[:self._vad_frames_num_bytes])
        if self._diagnostic_counter % 30 == 0:  # Log every ~1 second
            logger.debug(f"VAD: conf={confidence:.3f}, vol={volume:.3f}, "
                        f"state={self._vad_state}, starting={self._vad_starting_count}")
        return super()._run_analyzer(buffer)
```

### Architectural Improvements

#### 6. Consider using Soniox's own VAD/endpoint detection more directly

Since Soniox is already detecting speech (it produces interim transcriptions), you could:
- Set `vad_force_turn_endpoint=False` to let Soniox handle turn detection
- Use Soniox's `<end>`/`<fin>` tokens as the primary speech boundary signals
- Keep Silero VAD only for interruption detection (less critical timing)

This would decouple transcription flow from Silero VAD's potentially slow detection.

#### 7. Consider a dual-signal approach

Instead of relying solely on Silero VAD, combine signals:
- Silero VAD confidence (model-based)
- Simple amplitude/RMS threshold (fast, no smoothing delay)
- Soniox interim transcription presence (proof of speech)

If any two of these three signals indicate speech, trigger "User started speaking".

---

## Testing Recommendations

1. **A/B test** the config changes (min_volume=0.2, confidence=0.4) against production config
2. **Log actual volume and confidence values** on a sample of calls to understand the distribution
3. **Measure false positive rate** (VAD triggering on non-speech) with lower thresholds
4. **Test with diverse phone types**: Feature phones, smartphones, different carriers, different ambient environments

---

## Files Referenced

| File | Purpose |
|------|---------|
| `pipecat/audio/vad/vad_analyzer.py` | VAD state machine and detection algorithm |
| `pipecat/audio/vad/silero.py` | Silero ONNX model wrapper |
| `pipecat/audio/utils.py:153-177` | EBU R128 volume calculation |
| `pipecat/transports/base_input.py:408-471` | Audio processing loop and VAD invocation |
| `pipecat/transports/base_input.py:537-566` | "User started/stopped speaking" events |
| `pipecat/services/soniox/stt.py:250-263` | Soniox VAD integration (finalize on stop) |
| `app/ai/voice/agents/breeze_buddy/agent/vad.py` | VAD analyzer creation |
| `app/ai/voice/agents/breeze_buddy/template/vad.py` | Runtime VAD param management (bug location) |
| `app/ai/voice/agents/breeze_buddy/processors/response_gate.py` | Interruption state machine |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` | Pipeline construction |
