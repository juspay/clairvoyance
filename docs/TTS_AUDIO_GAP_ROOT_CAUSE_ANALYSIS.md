# TTS Audio Gap Root Cause Analysis — BB Daily Mode

**Date**: 2026-05-05
**Branch**: add-filler-words-and-function-call-bg-music
**Status**: Post-rebase analysis (pipecat-ai 1.1.0, daily-python 0.28.0)

---

## Problem Statement

TTS audio has audible gaps/breaks in Breeze Buddy's Daily (WebRTC) mode. The issue is:
- TTS-provider-agnostic (ElevenLabs + Cartesia both affected)
- Occurs even with NO background sound mixer and NO function call background music
- Affects Daily mode significantly more than telephony (Twilio/Plivo/Exotel)
- Affects BB more than the Automatic agent

---

## Diagnostic Tests Run

13 tests across 6 test classes in `tests/test_audio_gap_diagnosis.py`. All pass.

| Test Class | What It Tests | Result |
|------------|---------------|--------|
| TestEventLoopContention | Does BB's extra tasks starve audio output? | **ELIMINATED** — max gap 1.1ms even with 80 competing tasks |
| TestSOXRResampler | Does resampler state clearing cause discontinuities? | **ELIMINATED** — discontinuity ratio 0.19 (threshold 2.0) |
| TestDailySDKCallbackLatency | Does `call_soon` / `write_frames` callback delay? | **ELIMINATED** — P99 0.35ms under 30 competing tasks |
| TestBaselinePipelineJitter | Baseline frame delivery jitter | **ELIMINATED** — sub-ms jitter in ideal conditions |
| TestInterContextSilence | 500ms silence between TTS audio contexts | **CONFIRMED** (now fixed by pipecat 1.1.0) |
| TestAggregateSentencesImpact | aggregate_sentences creates more context boundaries | **REVISED** — not a cause post-rebase (sentence aggregation is beneficial) |

---

## Root Causes

### FIXED by Rebase (pipecat-ai 0.0.102 → 1.1.0, daily-python 0.23.0 → 0.28.0)

#### RC-1: 500ms Inter-Context Silence — FIXED
**Severity**: HIGH — was the #1 cause of audible gaps
**Status**: FIXED in pipecat-ai 1.1.0

The old `AudioContextTTSService._audio_context_task_handler` (tts_service.py:1171) appended 500ms of silence (`b"\x00" * self.sample_rate`) between every TTS audio context (sentence). BB's structured LLM responses produce many short contexts → many 500ms pauses.

In pipecat-ai 1.1.0:
- The `silence = b"\x00" * self.sample_rate` line is completely removed
- `AudioContextTTSService` is deprecated (now a thin wrapper around `WebsocketTTSService`)
- No inter-context silence is injected

#### RC-2: Outdated daily-python SDK — FIXED
**Severity**: HIGH — missing gapless audio improvements
**Status**: FIXED — daily-python now at 0.28.0

Was 5 versions behind (0.23.0). Version 0.28.0 includes "gapless audio for raw-tracks" (Daily changelog #073, Dec 2025) with WebRTC audio pipeline improvements for raw audio tracks.

---

### STILL PRESENT (Active Root Causes)

#### RC-3: No Audio Pacing in `without_mixer` Path — ACTIVE
**Severity**: MEDIUM-HIGH — likely the primary remaining cause
**Status**: NOT FIXED in pipecat-ai 1.1.0

**File**: `base_output.py:761` (in pipecat-ai package)
**Code**:
```python
async def without_mixer(vad_stop_secs: float) -> AsyncGenerator[Frame, None]:
    while True:
        try:
            frame = await asyncio.wait_for(
                self._audio_queue.get(), timeout=vad_stop_secs
            )
            yield frame
            self._audio_queue.task_done()
        except TimeoutError:
            await self._bot_stopped_speaking()
```

When no background sound mixer is configured (most BB Daily calls), the output path has **zero pacing**:
- Frames are pulled from queue and written to `CustomAudioSource.write_frames()` as fast as they arrive
- No jitter buffer, no pre-buffering, no rate control
- TTS produces audio in bursts → Daily SDK's WebRTC play cursor catches up during delivery delays → gaps
- The `with_mixer` path at least has `await asyncio.sleep(0)` between iterations; `without_mixer` has nothing
- Default chunk size is 40ms (`audio_out_10ms_chunks=4`) — if one write is delayed, the gap is proportionally larger

**Why Automatic is less affected**: Automatic typically has `allow_interruptions=True` and a simpler pipeline with fewer processors. Fewer processors = frames flow through faster = less bursty delivery.

**Audio flow**:
```
TTS audio frames
  → BaseOutputTransport.MediaSender.handle_audio_frame()
    → resample + buffer in bytearray
    → chunk into 40ms pieces (4 x 10ms default)
    → put into _audio_queue
  → _next_frame() / without_mixer
    → yield immediately (NO pacing)
  → _audio_task_handler()
    → DailyOutputTransport.write_audio_frame()
      → CustomAudioSource.write_frames() [daily-python native]
      → await completion callback
```

---

### NOT A ROOT CAUSE (Revised)

#### `aggregate_sentences=True` — NOT a cause (beneficial to keep)
Keeping `aggregate_sentences=True` is actually better now that the 500ms inter-context silence is gone:
- Each sentence produces one continuous audio stream (fewer fragment boundaries)
- Sentence-level prosody optimization = better TTS quality
- Fewer TTS requests overall (one per sentence vs one per token)
- Setting `aggregate_sentences=False` would create MORE fragment boundaries with startup overhead per fragment, potentially worsening gaps

---

### ELIMINATED (Not Root Causes)

| Cause | Evidence |
|-------|----------|
| Silero VAD blocking event loop | `analyze_audio()` uses `run_in_executor` in both BB and Automatic — already offloaded to thread pool |
| Event loop contention from BB's extra tasks | Max gap 1.1ms even with 80 competing tasks (test confirmed) |
| SOXR resampler discontinuity | Discontinuity ratio 0.19, well below 2.0 threshold |
| Daily SDK callback latency | P99 0.35ms under 30 competing tasks |
| `aggregate_sentences=True` bursty delivery | Not a cause post-rebase — sentence-level aggregation produces smoother continuous audio |

---

## Fixes — Ordered by Priority

### Fix 1: Reduce Audio Chunk Size (Priority: HIGH, Effort: TRIVIAL)

**What**: Set `audio_out_10ms_chunks=2` in DailyParams (20ms chunks instead of 40ms)
**Why**: Smaller, more frequent writes to Daily SDK → smoother cadence. If one write is delayed by 10ms, the gap is only 20ms total instead of 40ms — proportionally less audible. Same audio content, same playback speed, just smaller write granularity.
**File**: `app/ai/voice/agents/breeze_buddy/agent/transport.py`
**Change**:
```python
# Before
"daily": lambda: DailyParams(
    audio_in_enabled=True,
    audio_out_enabled=True,
    audio_in_filter=audio_in_filter,
    audio_out_mixer=daily_mixer,
),

# After
"daily": lambda: DailyParams(
    audio_in_enabled=True,
    audio_out_enabled=True,
    audio_in_filter=audio_in_filter,
    audio_out_mixer=daily_mixer,
    audio_out_10ms_chunks=2,  # 20ms chunks for smoother Daily WebRTC cadence
),
```
**Risk**: More `write_frames` calls per second (50 vs 25), slightly more overhead. Negligible.
**Rollback**: Remove the parameter (defaults to 4)

---

### Fix 2: Add Pre-Buffering Before Audio Output (Priority: MEDIUM, Effort: MEDIUM)

**What**: Buffer 2-3 audio chunks (40-60ms) before starting to write to Daily SDK
**Why**: Gives the Daily SDK a head start so small delivery delays don't starve the WebRTC play cursor. The SDK has an internal buffer that absorbs jitter once it has some runway — the problem is the initial burst where the play cursor starts immediately with zero buffer.
**File**: New file `app/ai/voice/agents/breeze_buddy/utils/audio_pacing.py`
**Approach**: Wrap the Daily transport's `write_audio_frame` to buffer initial chunks before streaming
**Risk**: Adds 40-60ms initial TTS latency; acceptable for voice calls
**Rollback**: Remove the wrapper

---

### Fix 3: Pipeline-Level Pacing via `asyncio.sleep` (Priority: LOW, Effort: LOW-MEDIUM)

**What**: Add a minimal `asyncio.sleep(0)` yield in the `without_mixer` path (same as `with_mixer` path)
**Why**: Gives other tasks a chance to run between audio frame deliveries, preventing burst delivery
**File**: Monkey-patch or subclass `BaseOutputTransport.MediaSender._next_frame` in pipecat
**Approach**:
```python
async def without_mixer(vad_stop_secs):
    while True:
        try:
            frame = await asyncio.wait_for(
                self._audio_queue.get(), timeout=vad_stop_secs
            )
            yield frame
            self._audio_queue.task_done()
            await asyncio.sleep(0)  # yield to event loop
        except TimeoutError:
            await self._bot_stopped_speaking()
```
**Risk**: Minimal — `sleep(0)` just yields control to other tasks; doesn't delay audio
**Rollback**: Remove the patch

---

## Verification Plan

After each fix:
1. Run diagnostic tests: `.venv/bin/python -m pytest tests/test_audio_gap_diagnosis.py -v -s`
2. Test with a real Daily call (playground mode) — listen for gaps
3. Test with a telephony call — verify no regression
4. Test with each TTS provider (ElevenLabs, Cartesia)

## Recommended Implementation Order

1. **Fix 1** (chunk size) — trivial one-liner, safe, immediate
2. **Fix 2** (pre-buffering) — more involved, test after above
3. **Fix 3** (asyncio.sleep yield) — lowest priority, least certain impact

## Version History

| Component | Before Rebase | After Rebase |
|-----------|--------------|-------------|
| pipecat-ai | 0.0.102 | 1.1.0 |
| pipecat-ai-flows | 0.0.22 | 1.0.0 |
| daily-python | 0.23.0 | 0.28.0 |
