# Pipecat Upgrade & Optimization Recommendations for Breeze Buddy

**Date**: 2026-03-18
**Scope**: Cross-referencing pipecat v0.0.101–v0.0.105 release notes against the Clairvoyance/Breeze Buddy codebase
**Goal**: Identify changes that improve **reliability** and reduce **latency** without breaking current functionality

---

## Table of Contents

1. [Latency Improvements](#1-latency-improvements)
2. [Reliability Improvements](#2-reliability-improvements)
3. [Deprecation Fixes (Breaking Risk)](#3-deprecation-fixes-breaking-risk)
4. [Observability & Debugging Enhancements](#4-observability--debugging-enhancements)
5. [Architecture Improvements](#5-architecture-improvements)
6. [Nice-to-Have / Future Considerations](#6-nice-to-have--future-considerations)
7. [Do NOT Touch (Risk > Reward)](#7-do-not-touch-risk--reward)

---

## 1. Latency Improvements

### 1.1 Enable Concurrent Audio Context for Cartesia TTS (v0.0.105)
**Impact: HIGH | Risk: LOW**

Pipecat v0.0.105 added concurrent audio context support to `CartesiaTTSService`. By setting `pause_frame_processing=False`, Cartesia can synthesize the *next* sentence while the *previous* one is still playing — effectively pipelining TTS.

**Current state** (`app/ai/voice/tts/cartesia.py:64-70`):
```python
return CartesiaTTSService(
    api_key=config.api_key,
    voice_id=config.voice_id,
    model=config.model,
    params=params,
    aggregate_sentences=config.aggregate_sentences,
)
```

**Recommendation**: Add `pause_frame_processing=False` to `CartesiaTTSService` constructor. This is the single highest-impact latency reduction available — it eliminates the inter-sentence gap where the pipeline waits for one sentence to finish before synthesizing the next.

**Files to change**: `app/ai/voice/tts/cartesia.py`

---

### 1.2 Enable LLM Retry on Timeout (v0.0.102+)
**Impact: HIGH | Risk: LOW**

The codebase already has a TODO for this (`app/ai/voice/agents/breeze_buddy/agent/pipeline.py:105-108`):
```python
# TODO: Add retry_on_timeout=True, retry_timeout_secs=3.0 to reduce P99 tail latency
```

`BaseOpenAILLMService` has supported `retry_on_timeout` and `retry_timeout_secs` since at least v0.0.101. Enabling these will automatically retry LLM calls that exceed the timeout, reducing P99 tail latency from 500-1500ms Azure cold-starts.

**Recommendation**: Enable with conservative settings:
```python
llm = AzureLLMService(
    ...
    retry_on_timeout=True,
    retry_timeout_secs=3.0,
)
```

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

---

### 1.3 Use `system_instruction` for LLM Services (v0.0.105)
**Impact: MEDIUM | Risk: LOW**

v0.0.105 wired up `system_instruction` in `BaseOpenAILLMService` and `AzureLLMService`. This allows setting a default system prompt at the service level without polluting the `LLMContext`. The system instruction is injected at inference time, meaning:

- Cleaner context management (system prompt not in message history)
- System prompt doesn't count against context summarization thresholds
- Enables sharing one `LLMContext` across multiple LLM services

**Current state**: The Breeze Buddy flow manager sets system prompts via `LLMContext` messages in node configurations.

**Recommendation**: Evaluate moving the base system instruction (e.g., agent persona, language rules) to `system_instruction` parameter on `AzureLLMService`, while keeping node-specific instructions in context. This reduces context size per turn.

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

---

### 1.4 Enable `push_empty_transcripts` for OpenAI STT (v0.0.105)
**Impact: MEDIUM | Risk: LOW**

v0.0.105 added `push_empty_transcripts` to `BaseWhisperSTTService` and `OpenAISTTService`. When VAD fires but the user didn't actually speak (e.g., background noise), this lets the agent resume speaking instead of waiting indefinitely for a transcription that will never come.

**Current state**: When using OpenAI STT (`app/ai/voice/agents/breeze_buddy/stt/__init__.py:80-85`), empty transcripts are silently discarded, potentially causing the bot to hang after false VAD triggers.

**Recommendation**: Enable `push_empty_transcripts=True` when building OpenAI STT:
```python
return build_openai_stt(
    api_key=OPENAI_STT_API_KEY,
    model=OPENAI_STT_MODEL,
    language=Language.EN,
    temperature=0.0,
    push_empty_transcripts=True,
)
```

**Files to change**: `app/ai/voice/stt/openai.py`, `app/ai/voice/agents/breeze_buddy/stt/__init__.py`

---

### 1.5 httpx Connection Pooling for LLM (existing TODO)
**Impact: MEDIUM | Risk: LOW**

The codebase already has a TODO (`pipeline.py:107-109`):
```python
# TODO: Override create_client() to add httpx connection pooling (keepalive_expiry=None,
#       max_keepalive_connections=100) to avoid TCP+TLS cold-start on first request (50-200ms).
```

This is independent of pipecat version but worth implementing. Override `create_client()` on `AzureLLMService` to pass a custom `httpx.AsyncClient` with connection pooling.

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` or create a thin subclass.

---

### 1.6 Use `TextAggregationMode.TOKEN` for TTS (v0.0.104)
**Impact: MEDIUM | Risk: MEDIUM**

v0.0.104 added `text_aggregation_mode` parameter to `TTSService` with `TextAggregationMode.TOKEN` mode. In TOKEN mode, text is sent to TTS token-by-token instead of waiting for complete sentences. For WebSocket-based TTS (Cartesia, ElevenLabs), this can reduce time-to-first-audio by sending text as soon as LLM produces it.

**Trade-off**: TOKEN mode may produce slightly worse prosody for sentence boundaries. SENTENCE mode (current default) produces better quality but adds latency waiting for sentence completion.

**Recommendation**: Test TOKEN mode with Cartesia (which handles streaming well) on telephony calls where latency is critical. Keep SENTENCE mode for Daily (web) where quality matters more.

**Files to change**: `app/ai/voice/tts/cartesia.py`, `app/ai/voice/tts/elevenlabs.py`

---

### 1.7 DeepgramTTSService Interruption Handling (v0.0.105)
**Impact: LOW | Risk: NONE (informational)**

v0.0.105 changed `DeepgramTTSService` to send a `Clear` message on interruption instead of disconnecting/reconnecting the WebSocket. If you ever add Deepgram TTS, this means faster interruption recovery. Currently not applicable since Breeze Buddy uses Cartesia/ElevenLabs/Sarvam.

---

## 2. Reliability Improvements

### 2.1 Add Context Summarization (v0.0.102+)
**Impact: CRITICAL | Risk: MEDIUM**

The codebase already has a TODO (`pipeline.py:194`):
```python
# TODO: Add a breeze-buddy-specific context summarizer.
```

Since v0.0.102, pipecat has built-in context summarization via `LLMContextSummarizationConfig` (v0.0.102) and the improved `LLMAutoContextSummarizationConfig` (v0.0.104). This automatically compresses conversation history when token limits are reached.

**Why critical**: Long Breeze Buddy calls (especially with retries, idle prompts, multi-node flows) can blow past context windows, causing LLM errors or degraded responses.

**Recommendation (v0.0.104+ API)**:
```python
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAutoContextSummarizationConfig,
    LLMContextSummaryConfig,
)

context_aggregator = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        user_turn_strategies=user_turn_strategies,
        vad_analyzer=vad_analyzer,
    ),
    assistant_params=LLMAssistantAggregatorParams(
        enable_auto_context_summarization=True,
        auto_context_summarization_config=LLMAutoContextSummarizationConfig(
            max_context_tokens=3000,
            max_unsummarized_messages=20,
            summary_config=LLMContextSummaryConfig(
                target_context_tokens=1500,
                summarization_prompt="Summarize this conversation preserving key decisions, order details, and customer intent.",
            ),
        ),
    ),
)
```

**v0.0.104 bonus**: Can route summarization to a cheaper/faster LLM via `llm` field in config.

**v0.0.104 bonus**: `summarization_timeout=120s` prevents hung LLM calls from blocking summarization permanently.

**v0.0.105 bonus**: `system_instruction` parameter on `run_inference` means summarization uses its own prompt cleanly.

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

---

### 2.2 Add ServiceSwitcherStrategyFailover for TTS (v0.0.105)
**Impact: HIGH | Risk: MEDIUM**

v0.0.105 added `ServiceSwitcherStrategyFailover` that automatically switches to a backup service when the active one reports a non-fatal error. Breeze Buddy already supports multiple TTS providers (Cartesia, ElevenLabs, Sarvam) — this would let you configure automatic failover.

**Use case**: If Cartesia has a transient outage during a call, automatically switch to ElevenLabs without dropping the call.

**Recommendation**: Wrap TTS services in a `ServiceSwitcher` with failover strategy:
```python
from pipecat.services.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyFailover

tts_primary = await get_cartesia_tts_service(...)
tts_fallback = await get_elevenlabs_tts_service(...)

tts = ServiceSwitcher(
    services=[tts_primary, tts_fallback],
    strategy=ServiceSwitcherStrategyFailover(),
)
```

**Files to change**: `app/ai/voice/agents/breeze_buddy/tts/__init__.py`, `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

---

### 2.3 Add `function_call_timeout_secs` per Tool (v0.0.105)
**Impact: MEDIUM | Risk: LOW**

v0.0.105 added optional `timeout_secs` parameter to `register_function()` and `register_direct_function()` for per-tool timeout control. Breeze Buddy's flow manager registers multiple function handlers — some (like `end_conversation`) may need longer timeouts than others.

**Recommendation**: Set per-tool timeouts based on expected execution time:
- Fast tools (node transitions): 5s
- Medium tools (API calls to payment gateways): 10s
- Slow tools (end_conversation with DB writes + callbacks): 15s

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/flow.py` (where `register_function` calls happen)

---

### 2.4 Upgrade Deepgram SDK to v6 (v0.0.105 — if using Deepgram)
**Impact: HIGH (if applicable) | Risk: MEDIUM**

v0.0.105 updated `DeepgramSTTService` to use `deepgram-sdk v6`. The v6 SDK removed automatic keepalive — pipecat now sends explicit `KeepAlive` messages every 5 seconds to prevent disconnections.

**Current state**: Deepgram is in the pipecat extras list and shared STT implementations (`app/ai/voice/stt/deepgram.py`) but it's unclear if it's actively used by Breeze Buddy (Soniox and Sarvam seem primary).

**Recommendation**: If Deepgram is used or planned, ensure the SDK upgrade happens. Import `LiveOptions` from `pipecat.services.deepgram.stt` instead of from `deepgram` directly.

**Files to change**: `app/ai/voice/stt/deepgram.py`

---

### 2.5 ElevenLabs Context ID Reuse Fix (v0.0.103)
**Impact: MEDIUM | Risk: NONE (automatic with upgrade)**

v0.0.103 fixed context ID reuse issues in `ElevenLabsTTSService`, `CartesiaTTSService`, and others. Services now properly reuse the same context ID across multiple `run_tts()` invocations within a single LLM turn, preventing context tracking issues and incorrect lifecycle signaling.

**Why it matters**: This fixes potential issues where word timestamps get interleaved across sentences (also fixed in v0.0.103 for ElevenLabs).

**Recommendation**: Ensure pipecat is at least v0.0.103 to get these fixes automatically.

---

### 2.6 Fix UserIdleController False Triggers (v0.0.103)
**Impact: MEDIUM | Risk: NONE (automatic with upgrade)**

v0.0.103 fixed `UserIdleController` false idle triggers caused by gaps between user and bot activity frames. The idle timer now starts only after `BotStoppedSpeakingFrame` and is suppressed during active user turns and function calls.

**Current state**: Breeze Buddy has custom `UserIdleCallbackHandler` (`app/ai/voice/agents/breeze_buddy/processors/user_idle.py`) that wraps pipecat's `UserIdleProcessor`. The fix in v0.0.103 prevents false triggers that could prematurely end calls.

**Recommendation**: Upgrade pipecat to get this fix. No code changes needed.

---

### 2.7 Fix `push_interruption_task_frame_and_wait()` Hanging (v0.0.103)
**Impact: HIGH | Risk: NONE (automatic with upgrade)**

v0.0.103 fixed `push_interruption_task_frame_and_wait()` hanging indefinitely when the `InterruptionFrame` doesn't reach the pipeline sink within the timeout. Added a timeout keyword argument.

**Current state**: `ResponseStateGate` (`app/ai/voice/agents/breeze_buddy/processors/response_gate.py:139`) calls `push_interruption_task_frame_and_wait()` directly. If the interruption frame gets stuck (e.g., slow TTS), this could hang forever.

**Recommendation**:
1. Upgrade pipecat to v0.0.103+
2. Optionally add explicit timeout: `await self.push_interruption_task_frame_and_wait(timeout=5.0)`

**Files to change**: `app/ai/voice/agents/breeze_buddy/processors/response_gate.py`

---

### 2.8 AIC Filter Model Sharing (v0.0.103)
**Impact: LOW | Risk: NONE (automatic with upgrade)**

v0.0.103 added `AICModelManager` singleton that shares read-only AIC models across multiple filters with reference counting. Multiple filters using the same model path share one loaded model, with concurrent load deduplication and off-event-loop file I/O.

**Current state**: Breeze Buddy creates an AIC filter per call (`app/ai/voice/agents/breeze_buddy/agent/transport.py:58-62`). Without model sharing, each filter loads its own copy of the model.

**Recommendation**: Upgrade pipecat to get automatic model sharing. No code changes needed.

---

### 2.9 AIC Filter Enhancement Level with Runtime Control (v0.0.105)
**Impact: LOW | Risk: LOW**

v0.0.105 re-added `enhancement_level` support to `AICFilter` with runtime `FilterEnableFrame` control. The Breeze Buddy transport code (`transport.py:59-62`) creates the filter but doesn't currently set `enhancement_level`.

**Current state**: The static config has `AIC_ENHANCEMENT_LEVEL` but it's only used by the Automatic agent, not Breeze Buddy's transport factory.

**Recommendation**: Pass `enhancement_level` and `voice_gain` to the Breeze Buddy AIC filter:
```python
return AICFilter(
    license_key=static.BREEZE_BUDDY_AIC_LICENSE_KEY,
    model_path=Path(static.AIC_MODEL_PATH),
    enhancement_level=static.AIC_ENHANCEMENT_LEVEL,
    voice_gain=static.AIC_VOICE_GAIN,
    noise_gate_enable=static.AIC_NOISE_GATE_ENABLE,
)
```

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/transport.py`

---

## 3. Deprecation Fixes (Breaking Risk)

### 3.1 Deprecated Google Module Paths (v0.0.105)
**Impact: LOW | Risk: LOW**

v0.0.105 deprecated `pipecat.services.google.llm_vertex`, `pipecat.services.google.llm_openai`, and `pipecat.services.google.gemini_live.llm_vertex` modules. The old paths still work but emit `DeprecationWarning`.

**Recommendation**: If any imports use these old paths, update to the new paths:
- `pipecat.services.google.vertex.llm`
- `pipecat.services.google.openai.llm`
- `pipecat.services.google.gemini_live.vertex.llm`

---

### 3.2 Deprecated `AudioContextTTSService` Hierarchy (v0.0.105)
**Impact: NONE (informational)**

v0.0.105 deprecated `AudioContextTTSService`, `AudioContextWordTTSService`, `WordTTSService`, `WebsocketWordTTSService`, and `InterruptibleWordTTSService`. These are internal pipecat classes — Breeze Buddy doesn't subclass them directly, so no action needed.

---

### 3.3 Removed `supports_word_timestamps` Parameter (v0.0.105)
**Impact: NONE (informational)**

v0.0.105 removed `supports_word_timestamps` from `TTSService.__init__()`. Word timestamp logic is now always active. If any custom TTS subclass passes this parameter, it will break. Breeze Buddy uses stock TTS services, so no impact.

---

### 3.4 VAD `stop_secs` Default Change (v0.0.102)
**Impact: MEDIUM | Risk: MEDIUM**

v0.0.102 changed the default `VADParams.stop_secs` from 0.8s to 0.2s. This is a significant behavior change:
- **Before**: 0.8s silence before VAD considers speech stopped
- **After**: 0.2s silence — much more aggressive

**Current state**: Breeze Buddy sets `stop_secs` explicitly via dynamic config (`BB_DAILY_VAD_STOP_SECS`, `BB_TELEPHONY_VAD_STOP_SECS`), so the default change doesn't affect it. However, verify that your config values are intentional and not relying on the old default.

**Recommendation**: No action needed if explicit values are set. Document the default change for awareness.

---

### 3.5 Renamed `TranscriptionUserTurnStopStrategy` → `SpeechTimeoutUserTurnStopStrategy` (v0.0.102)
**Impact: NONE (already using new name)**

Breeze Buddy already imports `SpeechTimeoutUserTurnStopStrategy` (`pipeline.py:31`). No action needed.

---

## 4. Observability & Debugging Enhancements

### 4.1 Add `StartupTimingObserver` (v0.0.104)
**Impact: MEDIUM | Risk: NONE**

v0.0.104 added `StartupTimingObserver` for measuring how long each processor's `start()` method takes during pipeline startup, plus transport readiness timing. This is invaluable for debugging slow call setup.

**Recommendation**: Add to observers list:
```python
from pipecat.observers.startup_timing_observer import StartupTimingObserver

def get_observers():
    observers = [StartupTimingObserver()]
    if ENVIRONMENT.lower() == "dev":
        observers.extend([MetricsLogObserver(), ...])
    return observers
```

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

---

### 4.2 Add `UserBotLatencyObserver` with `on_latency_breakdown` (v0.0.104)
**Impact: MEDIUM | Risk: NONE**

v0.0.104 added `on_latency_breakdown` event to `UserBotLatencyObserver` providing per-service TTFB, text aggregation, user turn duration, and function call latency metrics. Also added `on_first_bot_speech_latency` for measuring time from client connection to first bot speech.

**Current state**: `UserBotLatencyLogObserver` is included but only in dev mode (`pipeline.py:67`).

**Recommendation**: Enable `UserBotLatencyLogObserver` in production (it's lightweight logging) and wire up the `on_latency_breakdown` event for Langfuse/OTel reporting:
```python
latency_observer = UserBotLatencyLogObserver()

@latency_observer.event_handler("on_latency_breakdown")
async def on_latency(observer, breakdown):
    # Log to Langfuse or emit as OTel metric
    pass
```

**Files to change**: `app/ai/voice/agents/breeze_buddy/agent/pipeline.py`

---

### 4.3 Add `TextAggregationMetricsData` (v0.0.104)
**Impact: LOW | Risk: NONE**

v0.0.104 added `TextAggregationMetricsData` measuring the time from first LLM token to first complete sentence. This represents the latency cost of sentence aggregation in the TTS pipeline. Available automatically when `enable_metrics=True` (already set in `pipeline.py:318`).

**Recommendation**: No code changes needed — just be aware this metric is now available in the metrics observer output.

---

### 4.4 Exposed `on_summary_applied` Event (v0.0.105)
**Impact: LOW | Risk: NONE**

v0.0.105 exposed `on_summary_applied` event on `LLMAssistantAggregator` for observing context summarization events. Useful for tracking when and how often summarization occurs.

**Recommendation**: If context summarization is implemented (see 2.1), wire this event for logging/alerting.

---

### 4.5 `ClientConnectedFrame` for Transport Readiness (v0.0.104)
**Impact: LOW | Risk: NONE**

v0.0.104 added `ClientConnectedFrame` pushed by all transports when a client connects. Combined with `StartupTimingObserver`, this enables measuring transport readiness timing.

**Recommendation**: No code changes needed — available automatically with newer pipecat.

---

## 5. Architecture Improvements

### 5.1 Use Strongly-Typed Settings for Runtime Updates (v0.0.104)
**Impact: MEDIUM | Risk: LOW**

v0.0.104 added strongly-typed settings classes (`DeepgramSTTSettings`, `CartesiaTTSSettings`, etc.) for runtime settings updates. Instead of dicts:
```python
# Old (error-prone)
await task.queue_frame(STTUpdateSettingsFrame(settings={"language": Language.ES}))

# New (type-safe)
await task.queue_frame(STTUpdateSettingsFrame(delta=DeepgramSTTSettings(language=Language.ES)))
```

**Current state**: Breeze Buddy's template VAD config uses dynamic reconfiguration (`template/vad.py`). If STT/TTS settings are ever updated at runtime, the typed API is safer.

**Recommendation**: Use typed settings classes for any runtime service configuration updates.

---

### 5.2 Use `broadcast_interruption()` Instead of `push_interruption_task_frame_and_wait()` (v0.0.102+)
**Impact: MEDIUM | Risk: MEDIUM**

v0.0.104 added `broadcast_interruption()` to `FrameProcessor`. This pushes an `InterruptionFrame` both upstream and downstream directly, avoiding the round-trip through the pipeline task that `push_interruption_task_frame_and_wait()` required.

**Current state**: `ResponseStateGate` (`response_gate.py:139`) uses `push_interruption_task_frame_and_wait()`.

**Trade-off**: `broadcast_interruption()` is faster but has different semantics — it doesn't wait for the interruption to propagate. Consider whether the gate needs to wait for interruption completion before flushing buffered transcriptions.

**Recommendation**: Evaluate replacing `push_interruption_task_frame_and_wait()` with `broadcast_interruption()` in `ResponseStateGate` for faster interruption handling. Needs testing to ensure buffered transcription flush timing is still correct.

**Files to change**: `app/ai/voice/agents/breeze_buddy/processors/response_gate.py`

---

### 5.3 Move `ServiceSettingsUpdateFrames` to `UninterruptibleFrames` (v0.0.104)
**Impact: NONE (automatic)**

v0.0.104 made `ServiceSettingsUpdateFrames` uninterruptible, meaning user interruptions won't prevent settings changes from taking effect. No code changes needed.

---

### 5.4 On-Demand Context Summarization (v0.0.104)
**Impact: MEDIUM | Risk: LOW**

v0.0.104 added `LLMSummarizeContextFrame` to trigger on-demand context summarization from anywhere in the pipeline (e.g., from a function call tool). This is useful for Breeze Buddy's flow-based architecture where you might want to summarize after completing a major flow node.

**Recommendation**: After implementing auto-summarization (2.1), add on-demand summarization at key flow transitions:
```python
from pipecat.frames.frames import LLMSummarizeContextFrame

# In a flow handler after completing a complex node
await task.queue_frames([LLMSummarizeContextFrame()])
```

---

### 5.5 Use `direction` Parameter on `PipelineTask.queue_frame()` (v0.0.104)
**Impact: LOW | Risk: LOW**

v0.0.104 added optional `direction` parameter to `PipelineTask.queue_frame()` and `PipelineTask.queue_frames()`, allowing frames to be pushed upstream from the end of the pipeline. Currently, all `queue_frame` calls push downstream.

**Recommendation**: Keep in mind for future use cases where upstream frame injection is needed.

---

## 6. Nice-to-Have / Future Considerations

### 6.1 AssemblyAI U3 Pro with Built-in Turn Detection (v0.0.104)
If exploring new STT providers, AssemblyAI's U3 Pro model (added in v0.0.104) has built-in turn detection. v0.0.105 added `vad_threshold` parameter for tuning VAD sensitivity. Could be an alternative to Soniox's semantic endpoint detection.

### 6.2 Sarvam STT v3 Model Support (v0.0.102)
v0.0.102 added `saaras:v3` STT model and `bulbul:v3-beta` TTS model support for Sarvam AI. The STT model adds `mode` parameter (transcribe, translate, verbatim, translit, codemix) and prompt support. Check if your Sarvam config is using the latest models.

### 6.3 STT Runtime Settings Updates (v0.0.105)
v0.0.105 added runtime settings updates (via `STTUpdateSettingsFrame`) for Cartesia, Deepgram, ElevenLabs, Soniox STT. Previously, changing settings at runtime only stored new values without reconnecting. This could enable dynamic language switching mid-call.

### 6.4 Deepgram Flux Settings Mid-Stream (v0.0.105)
v0.0.105 allows Deepgram Flux STT settings to be updated mid-stream via `STTUpdateSettingsFrame` without triggering a reconnect — useful if you add Deepgram STT support.

### 6.5 OpenAI Realtime STT Service (v0.0.102)
v0.0.102 added `OpenAIRealtimeSTTService` for real-time streaming STT using OpenAI's Realtime API WebSocket. Supports local VAD and server-side VAD modes, noise reduction, and automatic reconnection. Could be an upgrade from the current batch-mode OpenAI STT.

### 6.6 `UserIdleTimeoutUpdateFrame` for Runtime Idle Control (v0.0.103)
v0.0.103 added `UserIdleTimeoutUpdateFrame` to enable/disable user idle detection at runtime. Breeze Buddy could use this to dynamically adjust idle timeouts during different flow nodes (e.g., longer timeout during payment verification).

### 6.7 Custom Video Track Support (v0.0.105)
v0.0.105 added custom video track support to Daily transport. Not relevant for telephony but could enable avatar/visual content for Daily (web) mode in the future.

---

## 7. Do NOT Touch (Risk > Reward)

### 7.1 `daily-python` Upgrade to v0.24.0 (v0.0.105)
v0.0.105 updated `daily-python` from `~=0.23.0` to `~=0.24.0`. This is a dependency-level change that should be handled by pipecat's dependency resolution, not manually. Let pipecat pull the right version.

### 7.2 Interruption Wait Event Refactor (v0.0.102)
v0.0.102 moved the interruption wait event from per-processor instance state to `InterruptionFrame` itself. This is an internal pipecat change. The `ResponseStateGate` uses `push_interruption_task_frame_and_wait()` which abstracts this — no code changes needed.

### 7.3 NLTK Version Bump (v0.0.104)
v0.0.104 bumped nltk from 3.9.1 to 3.9.3 for security. This should be handled via pipecat's dependency resolution.

---

## Implementation Priority Matrix

| # | Recommendation | Latency | Reliability | Effort | Priority |
|---|---------------|---------|-------------|--------|----------|
| 1.1 | Concurrent Cartesia TTS | +++ | | 1 line | P0 |
| 2.1 | Context Summarization | | +++ | ~30 lines | P0 |
| 1.2 | LLM Retry on Timeout | ++ | ++ | 2 lines | P0 |
| 2.7 | Fix interruption hang | | +++ | 0 (upgrade) | P0 |
| 2.6 | Fix UserIdle false triggers | | ++ | 0 (upgrade) | P0 |
| 2.2 | TTS Service Failover | | +++ | ~20 lines | P1 |
| 2.5 | ElevenLabs context fix | | ++ | 0 (upgrade) | P1 |
| 1.3 | system_instruction | + | + | ~5 lines | P1 |
| 4.1 | StartupTimingObserver | | + | ~3 lines | P1 |
| 2.3 | Per-tool timeouts | | ++ | ~10 lines | P1 |
| 1.4 | push_empty_transcripts | + | + | ~3 lines | P2 |
| 1.5 | httpx connection pooling | ++ | | ~15 lines | P2 |
| 4.2 | Latency breakdown events | | + | ~10 lines | P2 |
| 2.9 | AIC enhancement_level | | + | ~3 lines | P2 |
| 2.8 | AIC model sharing | | + | 0 (upgrade) | P2 |
| 1.6 | TOKEN aggregation mode | ++ | | ~3 lines | P3 (test first) |
| 5.2 | broadcast_interruption() | + | | ~5 lines | P3 (test first) |

**Legend**: `+++` = high impact, `++` = medium, `+` = low

---

## Minimum Pipecat Version Required

Based on the recommendations above, the minimum pipecat version to get all "free" bug fixes (items requiring 0 code changes) is **v0.0.105**. Key fixes included:

- **v0.0.103**: UserIdle false trigger fix, interruption hang fix, ElevenLabs/Cartesia context ID fix, AIC model sharing, audio context timeout fix
- **v0.0.104**: Strongly-typed settings, startup timing, latency breakdown events, text aggregation metrics, on-demand summarization
- **v0.0.105**: Concurrent Cartesia TTS, service failover, system_instruction, push_empty_transcripts, runtime STT updates, per-tool timeouts

**Recommendation**: Pin `pipecat-ai>=0.0.105` in `pyproject.toml` and run a full regression test.

---

## Quick Wins (< 1 hour total)

These changes can be made immediately with minimal risk:

1. **Add `pause_frame_processing=False` to Cartesia TTS** (1.1)
2. **Add `retry_on_timeout=True, retry_timeout_secs=3.0` to AzureLLMService** (1.2)
3. **Add timeout to `push_interruption_task_frame_and_wait()`** (2.7)
4. **Add `enhancement_level` to Breeze Buddy AIC filter** (2.9)
5. **Add `StartupTimingObserver` to observers** (4.1)
6. **Enable `UserBotLatencyLogObserver` in production** (4.2)
