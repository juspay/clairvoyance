# Pipecat 1.1.0 → 1.8.1: upgrade assessment for the Breeze Buddy cascading pipeline

**Date:** 2026-09-03
**Baseline:** `release` @ `719d88f` (clairvoyance), `pipecat-ai==1.1.0` (2026-04-27), `pipecat-ai-flows==1.0.0`
**Target:** `pipecat-ai==1.8.1` (tag `v1.8.1`, 2026-08-27, `c1d395802` on `main` at time of writing), Flows now inside `pipecat.flows`
**Method:** full read of the pipecat CHANGELOG span 1.2.0–1.8.1 (≈5,800 lines) and the Flows changelog 1.1.0–1.4.0, an AST-level inventory of every pipecat symbol this repo touches, and a source-level check of every private seam we depend on against the 1.8.1 tree.

This document supersedes `docs/pipecat-upgrade-recommendations.md`, which was written against 0.0.101–0.0.105 and predates the 1.x renames (several of its file references no longer exist).

---

## 0. Where we stand

| | Ours | Latest | Delta |
|---|---|---|---|
| pipecat-ai | 1.1.0 | 1.8.1 | 7 minor releases, 4 months, 499 files / +73k −10k lines in `src/pipecat` |
| pipecat-ai-flows | 1.0.0 | 1.4.0 (**final, frozen**) | Flows merged into `pipecat.flows` at pipecat 1.5.0; standalone package pins `pipecat-ai<1.5` and cannot coexist |
| aic-sdk | 2.2.0 | ~=3.1.0 (required by 1.8.x) | breaking SDK port (1.8.0) |
| daily-python | 0.28.0 | >=0.29.1 | dependency bump |
| websockets | 15.0.1 | >=13.1 (now core dep) | fine |
| Python | 3.11 | >=3.11 | fine |

**How the pipeline is built today** (`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`):

```
transport.input() → stt → TranscriptionGate → [KB] → user_aggregator → llm → tts → MetricsCollector → transport.output() → assistant_aggregator
```

- Telephony via `FastAPIWebsocketParams` at 8 kHz, serializer built by pipecat's private `_create_telephony_transport`.
- STT default Soniox (`stt-rt-v?` per config, `language_hints="en,hi"`, `vad_force_turn_endpoint=False`, custom subclass that re-implements `_connect_websocket` to inject `max_endpoint_delay_ms=500`).
- **VAD is off in production** (`BREEZE_BUDDY_ENABLE_VAD` defaults to `False`), so turn start is transcription-driven and `UserBotLatencyObserver` is deliberately not attached.
- Turn stop: `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)` or `TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3)` when SMART_TURN is selected.
- LLM: `AzureLLMService` (southindia, gpt-4.1-mini / gpt-4o class deployments, `service_tier="auto"`, `max_completion_tokens=50`, `function_call_timeout_secs=10`). No `retry_on_timeout`. HTTP/2 pooling exists only for chat mode.
- TTS default ElevenLabs `eleven_flash_v2_5` over the Indian-residency websocket, `language=EN_IN`, `text_aggregation_mode` SENTENCE by default (TOKEN via Redis flag), `EmojiTextFilter`. DragonTTS caching proxy in front when `enable_tts_caching`.
- Flows: `FlowManager(task=…)`, `FlowsFunctionSchema` handlers returning `(FlowResult|None, NodeConfig|None)`, extra keys stuffed into `NodeConfig`.

---

## 1. New features we can actually use (mapped to our code)

Grouped by where they land in our pipeline. Version in brackets.

### 1.1 Turn-taking and interruptions

| Feature | Version | Why it matters for us | Where it lands |
|---|---|---|---|
| **Proposed turn frames + `ExternalUserTurnStrategies`** — Soniox (with `vad_force_turn_endpoint=False`), AssemblyAI, Deepgram Flux, Cartesia Ink-2, Sarvam realtime now emit `ProposedUserStarted/StoppedSpeakingFrame` and recommend external strategies | 1.5.0, 1.8.0 | Lets the STT's own endpointer close the turn. 1.8.0 fixed a **flat ~0.5 s delay per turn** for exactly our Soniox configuration, but only on the external stop strategy. With pinned VAD/transcription strategies the STT's turn frames are now *ignored* (1.8.0 #5156). | `pipeline.py:376-461`, `template/interruption.py` |
| Soniox `endpoint_sensitivity` (v5), `max_endpoint_delay_ms`, `endpoint_latency_adjustment_level`, `should_interrupt` — all native `Settings` | 1.3.0–1.5.0 | Deletes our `SonioxSTTServiceWithEndpointDelay` subclass and its private `_connect_websocket` copy. | `stt/soniox/service.py`, `soniox/config.py` |
| `LocalSmartTurnAnalyzerV3`: no `transformers`, 566 MB → 60 MB RSS, 5 s → 0.3 s cold start, monotonic clocks, absolute-deadline safety net, release-on-verdict (`wait_for_transcript=False`) | 1.3.0, 1.7.0 | Smart-turn v3.2 supports **Hindi and Marathi** and runs in 10–100 ms on CPU. Now cheap enough per telephony pod. | `pipeline.py:340-371`, `template/types.py:132-164` |
| `on_user_turn_inference_triggered`, `deferred(...)`, `LLMTurnCompletionUserTurnStopStrategy`, `FilterIncompleteUserTurnStrategies`, single-token markers `●/◐/○` | 1.2.0, 1.8.0 | The framework hook for **speculative inference**: start the LLM on an early signal, let a second judge finalize. See §2. | new, `LLMUserAggregatorParams.user_turn_strategies` |
| `AudioVolumeTracker` (400 ms window) behind `VADParams.min_volume` | 1.8.0 | Fewer false speech-stops mid-word on soft Hindi phonemes; makes turning VAD back on safer. | `template/vad.py` |
| Fixes: delayed interruptions (#4434), `TTSService` deadlock with `pause_frame_processing` (#4431), dropped uninterruptible frames (#4435), phantom end-of-turn from stale smart-turn state (#4967), turn ending mid-utterance (#4983, #5043), one-LLM-call-per-fragment (#5159), `UserIdleTimeoutUpdateFrame` applied immediately (#5173), idle re-prompt with `run_llm=True` no longer swallowed (#5146) | 1.2.0–1.7.0 | Free on upgrade. #5146 directly affects our `UserIdleCallbackHandler` escalation ladder. | — |
| `FunctionCallUserMuteStrategy`, `MuteUntilFirstBotCompleteUserMuteStrategy` (releases on TTS error) | ≤1.8.0 | Alternatives to our ad-hoc `mute_stt()` during `_speak_and_wait`. | `template/interruption.py` |

### 1.2 STT

| Feature | Version | Notes |
|---|---|---|
| `AzureSTTService` final transcripts `finalized=True` → turn-stop fast path; `Settings(profanity="raw")` (changelog: use `raw` for non-English where Azure over-masks ordinary words) | 1.4.0 | If Azure STT is ever used for Hindi: both are must-set. |
| Soniox `stt-rt-v5` default, most-common-token language detection for code-mixed utterances (#4495) | 1.2.0, 1.4.0 | Hinglish: an utterance ending in an English token is no longer labelled `en`. |
| Sarvam `saaras:v4` (22 Indic + Indian/Global English), new `SarvamRealtimeSTTService` (`saaras:v3-realtime`, `endpointing="vad"|"manual"`) | 1.8.0 | **`saaras:v2.5`/`saarika:v2.5` and the `prompt` setting are removed** (see §4). |
| AssemblyAI `universal-3-5-pro`, `mode="min_latency"`, `continuous_partials`, `interruption_delay`, `voice_focus`, `agent_context` carry-over, `language_codes` (keeps code-switching within the set) | 1.4.0–1.7.0 | Hindi is not in the tier-1 steering list; treat as Indian-English option only. |
| Deepgram: first-word drop fix, 3-strike reconnect then unusable, sdk 6/7 | 1.3.0–1.8.0 | nova-3 supports Hindi code-switching natively. |
| `GeminiSTTService` (`gemini-3.5-transcribe-live`, auto language + `languages` hints), `GoogleSTTService` phrase-set adaptation | 1.8.0 | New Hindi candidate to benchmark. |
| STT websocket connect failures → non-fatal `ErrorFrame` + `ServiceSwitcher` failover (#4514); `is_usable` model | 1.3.0, 1.8.0 | STT fallback (Soniox → Deepgram) without dropping the call. |
| `STTUsageMetricsData.audio_seconds`, `STTService.supports_ttfs`, `ttfs_p99_latency` override | 1.3.0, 1.7.0 | Cost + latency accounting per provider. |

### 1.3 TTS

| Feature | Version | Notes |
|---|---|---|
| **`ElevenLabsDialogueTTSService`** for `eleven_v3` / `eleven_v3_conversational` (Text-to-Dialogue websocket) | 1.8.0 | See §3. |
| ElevenLabs: `close_context` at turn completion + `isFinal` → `TTSStoppedFrame` (#4433); **`alignment` instead of `normalizedAlignment`** so Devanagari is not written to context transliterated (#4424); keepalive 1008 race fix; `pause_frame_processing` watchdog; default model → `eleven_flash_v2_5` (we already pin it) | 1.2.0–1.7.0 | #4424 is the single most important Hindi fix in the span: before it, ElevenLabs word-timestamp mode could feed romanized Hindi back into the LLM context. |
| `TextAggregationMode.TOKEN` made correct: sentence regrouping for word tracking, `AggregatedFrameSequencer` per-context, duplicate boundary frames fixed | 1.6.0, 1.7.0 | Our `BB_AGGREGATE_SENTENCES=false` path is now safe to A/B. |
| Sarvam TTS `TTSStoppedFrame` on `final` (no more `stop_frame_timeout_s` lag) (#4639); dropped-audio-at-turn-start fix for Sarvam/Soniox (#4497) | 1.2.0, 1.4.0 | Kills dead air and clipped first syllables on Sarvam. |
| Azure TTS `force_locale` (SSML `<lang>` wrap for multilingual neural voices), `private_endpoint`, last-word fix | 1.3.0–1.7.0 | `force_locale` stops accent flips on Hinglish with `*MultilingualNeural` voices. |
| `text_transforms` (`VoiceFormatter`: numbers, currency, dates, units, acronyms, `replace_text`) with original text preserved in context | 1.5.0 | **English-only expansions.** Use `replace_text` for pronunciation fixes (replaces deprecated pronunciation dictionaries); do not enable number/date expansion on Hindi output. |
| TTFA metrics (`ttfa`, `ttfb`, `leading_silence`), `max_consecutive_zero_audio_contexts`, `pause_frame_processing` only while audio pending | 1.5.0, 1.8.0 | Measures how much of "TTS latency" is silence padding. |
| Services connect during `setup()`, concurrently | 1.8.0 | Free pre-connect: TTS/STT sockets are open before `StartFrame`. |

### 1.4 LLM

| Feature | Version | Notes |
|---|---|---|
| `AzureLLMService`: `/openai/v1` endpoint surface, `token_provider` (Entra ID), `api_version` deprecated (default now `2025-04-01-preview`) | 1.8.0 | New Azure features only land on v1. Our `_PooledAzureLLMService.create_client` must mirror the `_use_v1_api` branch. |
| `service_tier` already a first-class param (we pass `"auto"`) | ≤1.1.0 | One-word change to `"priority"` once the deployment qualifies. See §2.3. |
| `retry_on_timeout` / `retry_timeout_secs` (default 5.0) on the OpenAI base (and Google since 1.8.0) | ≤1.1.0 / 1.8.0 | We never enabled it. Re-issues a request that produced no first chunk in N seconds: the cheapest tail-latency fix without PTU. |
| `LLMSwitcher` + `ServiceSwitcherStrategyFailover` with sane semantics (switch only on `is_usable=False`), `reach_inactive_services` | 1.7.0, 1.8.0 | Azure → OpenAI direct or Gemini flash fallback without flapping. |
| `add_tool_change_messages=True` on `LLMContextAggregatorPair` | 1.2.0 | Flows changes tools per node; this mitigates calling removed tools / hallucinated tool-shaped text. |
| `LLMService.append_system_instruction()`; direct functions auto-registered from `LLMContext(tools=…)`; `@tool_options(cancel_on_interruption, timeout_secs)`; `cancellable_by_llm` | 1.3.0–1.8.0 | Per-tool timeouts for flow handlers. |
| Function-call timeout now **cancels** the handler (`CancelledError`) | 1.8.0 | Behavior change for our handlers, see §4. |
| `TTFATMetricsData` (`ttfat`, `thinking_time`), unified LLM TTFB definition | 1.8.0 | Makes Azure vs Gemini vs Claude TTFB comparable. |
| OpenAI Responses `reasoning.effort="none"` default for gpt-5+; Anthropic thinking off by default on Sonnet 5+, adaptive thinking | 1.6.0, 1.8.0 | Relevant if we move to gpt-5.x on Azure. |
| Prompt caching observability already present (`cache_read_input_tokens`); `prompt_cache_key` still via `extra` | — | `docs/AZURE_PROMPT_CACHING.md` plan still valid. |

### 1.5 Transport / telephony

| Feature | Version | Notes |
|---|---|---|
| `FastAPIWebsocketParams.ws_close_timeout` (0.5 s) — fixes ~10 s teardown stall on half-closed telephony sockets | 1.4.0 | Frees pods sooner per call. |
| `resampler_clear_after_secs` on Twilio/Plivo/Exotel serializers (default 0.2 s) | 1.5.0 | New default behavior; verify no artefacts on long silences. |
| `TransportParams.audio_out_write_timeout_secs` (10 s) → transport unusable | 1.8.0 | Interacts with our `NonClosingWebSocket` proxy; needs a `processor_unusable_policy`. |
| Exotel serializer sends `stream_sid` | 1.8.0 | Cosmetic. |
| `SingleClientWebsocketServerTransport` drains farewell audio before closing (#4964) | 1.6.0 | Goodbye `TTSSpeakFrame` no longer cut. |
| `allowed_origins`, HMAC token auth patterns for `/ws` | 1.4.0 | Pattern only; we run our own FastAPI app. |

### 1.6 Pipeline / infra / observability

| Feature | Version | Notes |
|---|---|---|
| `PipelineWorker` (was `PipelineTask`), `WorkerRunner`, `pipecat.workers` multi-worker bus, `JobContext` | 1.3.0+ | Aliases keep our code running; migrate names when convenient. |
| `PipelineWorker(setup_timeout_secs=20, start_timeout_secs=20, processor_unusable_policy=…)`, `on_setup_timeout`, `on_pipeline_timeout` | 1.8.0 | Defaults are new failure modes for us, see §4. |
| `PipelineFlushFrame`, `FrameProcessor.pause_processing_all_frames_until()`, `broadcast_interruption()` | 1.4.0, 1.8.0 | Useful for `_speak_and_wait` and transfer flows. |
| `StartupTimingObserver` (setup vs start split), `UserBotLatencyObserver.on_latency_breakdown`, `TextAggregationMetricsData` | 0.0.104+, 1.8.0 | We only attach `MetricsLogObserver` in prod. |
| OTEL: STT/TTS `metrics.ttfb` fixed and parented to turn span, LLM `output` on interrupted turns, TTS span text restored, `gen_ai.provider.name=azure.ai.openai`, audio token usage | 1.2.0, 1.6.0 | **Pre-1.2 TTS/STT TTFB numbers in Langfuse were wrong.** Re-baseline. |
| `pipecat.evals` (`pipecat eval`, latency budgets, LLM-judged criteria, DTMF turns) | 1.4.0+ | Text-mode scenarios can regression-test Hindi flows. |
| `pipecat.flows` merged; `NO_RESPONSE` sentinel; `@flows_tool_options`; `append_text_to_context` on `tts_say`/`end_conversation`; multi-worker handoff example | Flows 1.1–1.4 | See §4.2 for the behavior changes. |

---

## 2. Sub-second latency with a cascading pipeline (and no PTU)

### 2.1 What the field is doing (last ~6 months)

- **Pipelining, not sequencing.** The pipecat/NVIDIA Nemotron reference (Jan 2026) lands 500–700 ms voice-to-voice on a cascade by streaming every stage, emitting the LLM's *first* segment at a sentence boundary capped at ~24 tokens, and using adaptive TTS (streaming for the first segment, batch afterwards). Nothing exotic: short first sentence + streaming TTS.
- **Turn-end is the biggest lever, not the LLM.** Pipecat's own STT-latency guidance models responsiveness as `user stops → [TTFS] → final transcript → bot starts` and ships `stt-benchmark` for P50/P90/P99 TTFS per provider. Semantic/ML endpointing (smart-turn v3.2, Deepgram Flux, Cartesia Ink-2, Soniox v5 sensitivity) replaces fixed silence timeouts.
- **Speculative / early inference.** Research (LTS-VoiceAgent, RelayS2S) and pipecat's `on_user_turn_inference_triggered` + `deferred()` chain: start generating on a tentative stop, gate finalization on a second judge (LLM completion marker or turn model). Pipecat 1.8.0 made the completion markers single tokens (`●/◐/○`) to save decode steps.
- **Prompt-prefix caching + stable context** (80–200 ms TTFT and ~50% input cost on Azure Standard; our `AZURE_PROMPT_CACHING.md` already has the plan).
- **Priority processing / "Fast mode"** on Azure OpenAI (renamed July 2026): pay-as-you-go tier with a p50 latency target and 99% > 80–100 TPS commitments. This is the no-PTU answer to inconsistent latency. Details in §2.3.
- **Two-tier routing** (small fast model for short/acknowledgement turns, capable model for tool-heavy turns) and **semantic/filler caches** returning pre-synthesized audio (we already have DragonTTS, ~1 ms hit).
- **Regional co-location** of STT, LLM, TTS and telephony media (southindia for Azure Speech + OpenAI; ElevenLabs India residency; Sarvam in-country).
- **Warm everything**: sockets, VAD/ONNX models, TTS contexts, HTTP/2 pools. Pipecat 1.8.0 now connects all services concurrently during `setup()` and halved import time.

### 2.2 Where our turn budget goes today (estimated) and what changes it

| Stage | Today (est.) | Lever | After |
|---|---|---|---|
| Speech-end detection | VAD off in prod → wait for Soniox final (`max_endpoint_delay_ms=500`) plus pinned-strategy path that ignores Soniox's own turn frames (1.8.0) | Turn Silero VAD back on (min_volume now robust), `ExternalUserTurnStrategies` or smart-turn v3.2 with `wait_for_transcript`, Soniox `endpoint_sensitivity` | 150–350 ms |
| STT final transcript | Soniox TTFS (measure) | benchmark Soniox v5 vs Deepgram nova-3 vs Sarvam saaras:v4 on our Hindi calls with `stt-benchmark` | provider-dependent |
| LLM TTFT | Azure Standard, spiky 500–1,900 ms | priority tier, prompt caching, `retry_on_timeout`, HTTP/2 pool in voice subprocess, short-first-sentence prompt, failover | 250–450 ms p95 |
| Sentence aggregation | wait for first full sentence | TOKEN mode (now correct) for ElevenLabs flash, or prompt-cap the first sentence | −100–300 ms |
| TTS TTFB | flash v2.5 ~75 ms model + network; v3 conversational ~280 ms | keep flash / Sarvam for first segment; DragonTTS hit ~1 ms for fillers | 100–250 ms |
| Output | 8 kHz μ-law chunks | `audio_out_10ms_chunks`, trailing-chunk flush fix (1.6.0) | — |

Sub-second end-to-end is reachable only by pulling every lever; the two that move the needle most for us are turn-end detection and the Azure tier.

### 2.3 Azure without PTU: what to do

1. **Priority processing ("Fast mode")** — `service_tier="priority"` on the request (we already pass `service_tier`), or set at the deployment (`"properties": {"service_tier": "priority"}`). Requirements and caveats from the Microsoft doc (2026-07-13):
   - Deployment type **Global Standard** or **Data Zone Standard (US)**. Regional Standard deployments are not supported. `southindia` is in the Global Standard list.
   - Model versions 2025-12-01 or later, plus **gpt-4.1 (2025-04-14)**. Supported today: gpt-4.1, gpt-5.1, gpt-5.2, gpt-5.4, **gpt-5.4-mini** (99% > 100 TPS target), gpt-5.5, gpt-5.6 family. **gpt-4o and gpt-4.1-mini are not on the list.** If we are on gpt-4.1-mini or gpt-4o, this means a model move (gpt-5.4-mini is the natural successor for a `max_completion_tokens=50` voice agent; benchmark it).
   - Same quota as standard; billed at the priority rate only for requests actually served as priority (`service_tier` echoed in the response; pipecat exposes the raw usage/response so we can log it).
   - **Ramp-rate limit: >50% TPM growth within 15 min gets downgraded to standard.** Our cron dispatcher bursts calls; smooth the ramp or expect silent downgrades at campaign start.
   - Peak-period downgrades and >128k-token contexts also fall back to standard.
2. **Prompt caching** as designed in `docs/AZURE_PROMPT_CACHING.md` (freeze system prompt + tool order, `prompt_cache_key` via `extra`).
3. **`retry_on_timeout=True, retry_timeout_secs≈2.0`** on the voice `AzureLLMService`. A stalled first chunk is re-issued instead of waited out.
4. **HTTP/2 pooled client in the voice subprocess** (today only chat uses `_pools.py`); pre-warm at subprocess start so the first turn does not pay TCP+TLS.
5. **Failover** with `LLMSwitcher` + `ServiceSwitcherStrategyFailover`: Azure primary, OpenAI-direct or `gemini-3.6-flash` (with `retry_on_timeout`, `stream_idle_timeout_secs`) secondary. Note it switches on `is_usable=False`, not on slowness; slowness is handled by 3.
6. **Move the region-latency question out of guesswork**: `TTFATMetricsData` + unified TTFB + `UserBotLatencyObserver.on_latency_breakdown` into Langfuse, per provider, per region.

### 2.4 Speculative inference, concretely, with pipecat 1.8.1

```
user_turn_strategies=UserTurnStrategies(
    start=[VADUserTurnStartStrategy(), MinWordsUserTurnStartStrategy(min_words=…)],
    stop=[
        deferred(TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(...), wait_for_transcript=False)),
        LLMTurnCompletionUserTurnStopStrategy(config=UserTurnCompletionConfig(...)),
    ],
)
```

- The smart-turn strategy fires `on_user_turn_inference_triggered` early; the LLM starts generating with a completion marker in its instructions; on `●` the turn finalizes and the response continues, on `◐/○` the mixin re-prompts and no audio is spoken.
- Cost: one extra decode of a single token (1.8.0) and a marker-instruction block in the system prompt (test in Hindi; the default instructions are English).
- Flows interaction: 1.2.1 fixed the tool-call hang with `filter_incomplete_user_turns`; 1.7.0 fixed the silent-after-tool-call case. Both required.
- Alternative that avoids the LLM judge: `ExternalUserTurnStrategies` driven by Soniox v5 `endpoint_sensitivity` plus the 1.8.0 0.5 s-delay fix. Cheaper, less accurate on Hinglish trailing-off.

---

## 3. TTS: ElevenLabs v3 Conversational, Hindi/Hinglish, and what pipecat now handles

### 3.1 Eleven v3 Conversational facts (ElevenLabs docs, Sept 2026)

| | `eleven_flash_v2_5` (current) | `eleven_v3_conversational` | `eleven_v3` |
|---|---|---|---|
| Model latency | ~75 ms | **~280 ms** (excl. network) | higher; not for real-time |
| Languages | 32 (Hindi yes) | 70+ (Hindi yes) | 70+ |
| Transport | TTS websocket (`chunk_length_schedule`, `auto_mode`) | **Text-to-Dialogue websocket** `wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input` | same |
| First-audio threshold | configurable | **fixed server threshold ≈ 40 chars / 8 words** before first partial audio; `flush` to force | same |
| Voice settings | stability, similarity, style, speed, speaker boost | **`stability` only** | same |
| Audio tags (`[laughs]`, `[excited]`) | no | yes | yes |
| Voices per connection | 1 | **exactly 1** | up to 10 |
| Timestamps | alignment (word timestamps) | `sync_alignment=true` returns timing arrays | same |
| Idle timeout | — | 20 s without a message (keep-alive) | same |
| Concurrency | standard pool | dedicated pool, separate from standard limits | same |
| Access | GA | GA; ElevenLabs lists it among the recommended real-time models ("for the most expressive delivery") | GA |

pipecat 1.8.0 ships `ElevenLabsDialogueTTSService` for exactly these two models: forces `TextAggregationMode.SENTENCES`, resolves `pcm_8000/16000/24000` from the pipeline sample rate, registers the voice per context, handles the keep-alive/20 s rule, and reads only `stability`. The pipecat changelog is explicit that `ElevenLabsTTSService` with Flash stays lower-latency.

### 3.2 Verdict for our pipeline

- **Do not switch the default to v3 Conversational for latency.** Expect roughly +200–300 ms TTFB versus flash before network, and the 40-char / 8-word server buffer is hostile to short Hindi replies ("जी हाँ, ज़रूर बताइए" is ~4 words), so the first audio of short turns will wait for the sentence end or a flush.
- **Do pilot it for quality on the segments that need expressiveness** (greetings, empathy nodes, long explanations): the audio-tag control and 70-language model are real Hinglish improvements over flash's phonetic reading of Devanagari. Run it behind DragonTTS (`/tts/stream` tee) so repeated openers are cache hits regardless of model latency, and behind `ServiceSwitcher` failover to flash.
- **Benchmark against Sarvam `bulbul:v3`/v4 and Cartesia sonic-3 first.** An independent blind study reported bulbul v3 as most-preferred for Hindi at both 48 kHz and telephony 8 kHz, beating ElevenLabs v3 alpha, flash v2.5 and sonic-3; it handles code-switching in one pass and is tuned for 8 kHz. Sarvam has the lowest structural latency to Indian telephony too. Our Sarvam integration gets the 1.4.0 `TTSStoppedFrame` fix and the 1.2.0 dropped-first-audio fix on upgrade, which were the two complaints against it.
- **Measure with TTFA, not TTFB.** `leading_silence` will tell us how much of any provider's "latency" is silence padding, which differs a lot between ElevenLabs, Azure and Sarvam.

### 3.3 Hindi / Hinglish specifics now handled by pipecat (no local fixes needed)

- ElevenLabs writes **`alignment`** (original script) into context, not `normalizedAlignment` (1.2.0). Devanagari stays Devanagari.
- Soniox language labelling by majority token (1.2.0); Soniox v5; Sarvam saaras:v4 (Indic + Global English).
- Azure TTS `force_locale` for multilingual voices (1.7.0); Azure STT `profanity="raw"` (1.4.0).
- Word-timestamp tracking survives curly quotes/dashes, tagged spans, markup at sentence end (1.8.0).
- `elevenlabs_language_code()` now validates the `language_code` against the model's table and drops it with a warning if unsupported. **Check `Language.EN_IN`:** the 1.8.1 mapping table has `Language.EN → "en"` and `Language.HI → "hi"`; confirm on upgrade that `EN_IN` still resolves to `"en"` rather than being dropped (we pass `EN_IN` in four places).

### 3.4 Local fixes we keep, and ones that become redundant

| Ours | Status after upgrade |
|---|---|
| `EmojiTextFilter` | keep (pipecat has no emoji filter). Note `max_consecutive_zero_audio_contexts=3`: if the filter strips a whole turn to empty three times in a row, the TTS marks itself unusable (1.8.0). Set it to 0 or handle empties before the TTS. |
| DragonTTS proxy (`TTSService` subclass) | keep. `start()` should become `setup()` (services now connect in setup; `StartFrame.audio_*` fields deprecated). |
| ElevenLabs Indian-residency URL switch | keep. |
| `text_aggregation_mode` per provider | keep; TOKEN mode is now safe to enable for flash. |
| `SonioxSTTServiceWithEndpointDelay` | **delete** (native settings). |
| Pronunciation fixes, if any are added | use `text_transforms=[("*", replace_text(...))]`, not pronunciation dictionaries (deprecated 1.6.0, break word tracking). |
| Number/date reading in Hindi | do **not** enable `VoiceFormatter`/`expand_numbers` (English output). Keep prompt-level "write numbers as words" for Hindi. |

---

## 4. What stops working when we pull 1.8.1 (verified against source)

Legend: **HARD** = import or construction error; **SILENT** = runs but behaves differently; **PRIVATE** = we depend on a private seam that still exists but changed shape; **DEPRECATED** = warning now, removal at 2.0.

### 4.1 Dependencies and imports

| # | Item | Class | Where |
|---|---|---|---|
| 1 | `pipecat-ai-flows` cannot coexist with pipecat ≥1.5; drop it from `pyproject.toml` and change `pipecat_flows` → `pipecat.flows` (12 files; `FlowManager, FlowsDirectFunction, FlowsFunctionSchema, NodeConfig, ActionConfig, FlowResult` all present in `pipecat.flows`) | HARD | pyproject, `agent/flow.py`, `template/builder.py`, `global_function.py`, `chat/…`, `mcp/…` |
| 2 | `aic-sdk` 2.2 → ~=3.1: `AICFilter(license_key, model_id|model_path, model_download_dir, enhancement_level)`; env var `AIC_LICENSE_KEY` → `AIC_SDK_LICENSE`; energy VAD removed | HARD (SDK) | `agent/transport.py:35-56`, config |
| 3 | `SarvamSTTService.Settings(prompt=…)` removed; `saaras:v2.5`/`saarika:v2.5` removed; default model `saaras:v4`; `set_prompt()` gone | HARD | `app/ai/voice/stt/sarvam.py:89-102` |
| 4 | `daily-python` ≥0.29.1, `deepgram-sdk` <8, `websockets` core dep (`websockets-base` extra removed; we never listed it), `nltk` ≥3.10, `pydantic` unchanged for 3.11 | lock refresh | `uv lock` |
| 5 | `pipecat.services.settings` no longer exports `NOT_GIVEN/NotGiven/is_given` (moved to `pipecat.utils.types`) | n/a for us (we import only `TTSSettings`, still there) | — |
| 6 | `language_to_elevenlabs_language` moved to `tts_base.py` but re-exported from `elevenlabs.tts` | OK | `tts/elevenlabs.py` |
| 7 | `pipecat.processors.frameworks.rtvi` is now a package; `RTVIObserverParams`, `RTVIFunctionCallReportLevel`, `RTVIServerMessageFrame` re-exported | OK | `pipeline.py:26`, `agent/__init__.py:16` |
| 8 | `FunctionCallResultProperties` now lives in `frames.frames`; still importable from `services.llm_service` | OK | `mcp/__init__.py` |

### 4.2 Flows behavior changes (1.1 → 1.4 in-package)

| # | Change | Class |
|---|---|---|
| 9 | Initial node follows its `context_strategy` (default APPEND) instead of always resetting; set `RESET` on the initial node for old behavior | SILENT |
| 10 | `tts_say` / `end_conversation` actions append spoken text to LLM context by default (`append_text_to_context=True`) | SILENT |
| 11 | `FlowsFunctionSchema.handler` is required (constructing without raises) | HARD only if we construct schema-only entries; chat mode uses `FlowsFunctionSchema` as a container — verify each site passes a handler |
| 12 | 0/1-arg function handlers and 1-arg action handlers deprecated (`(args, flow_manager)` / `(action, flow_manager)`); `FlowResult` deprecated; `@flows_direct_function` → `@flows_tool_options` | DEPRECATED |
| 13 | `FlowManager(task=…)` and `.task` → `worker=` / `.worker` | DEPRECATED |
| 14 | Flows now relies on pipecat tool auto-registration (no `register_function`); `NO_RESPONSE` sentinel available | SILENT / feature |
| 15 | `NodeConfig` is still `TypedDict(total=False)`, so our extra keys (`vad_config`, `interruption`, `input_collection`) keep working | OK |

### 4.3 Pipeline / lifecycle defaults that change failure modes

| # | Change | Class | Our exposure |
|---|---|---|---|
| 16 | `TTSSpeakFrame.append_to_context` default `None→True` | SILENT | 20 `TTSSpeakFrame` sites: hold messages, greetings, idle re-prompts, `WidgetVoiceBridge`, `tts_say`. Every one now lands in LLM context unless `append_to_context=False`. Audit each. |
| 17 | Function call exceeding `function_call_timeout_secs` (ours: 10 s) is **cancelled** (`CancelledError` in the handler) then LLM re-runs | SILENT | Flow handlers with DB writes / callbacks must be cancellation-safe. |
| 18 | `PipelineWorker(setup_timeout_secs=20, start_timeout_secs=20)`; a service that fails to connect in `setup()` is permanently unusable; services connect in `setup()` not `StartFrame` | SILENT | Slow provider connect at call start now tears the pipeline down. DragonTTS `start()` → `setup()`. |
| 19 | `processor_unusable_policy` default `CONTINUE`; `TTSService.max_consecutive_zero_audio_contexts=3`; `TransportParams.audio_out_write_timeout_secs=10`; websocket services stop reconnecting once unusable | SILENT | A dead TTS/transport leaves a silent call instead of ending it. Set `ProcessorUnusablePolicy.END` and alert on `on_usable_changed`. |
| 20 | STT streaming services no longer emit `ProcessingMetricsData`; websocket TTS neither; LLM TTFB redefined (Anthropic/Google values rise) | SILENT | `MetricsCollectorProcessor` STT/TTS processing panels go empty; Langfuse baselines shift. |
| 21 | OTEL `gen_ai.provider.name` `az.ai.openai → azure.ai.openai`; `total_tokens` gross on Anthropic | SILENT | Langfuse filters / cost dashboards. |
| 22 | `FastAPIWebsocketParams.ws_close_timeout=0.5`, `resampler_clear_after_secs=0.2` on telephony serializers, `SingleClientWebsocketServerTransport` rename | SILENT | Re-test long-silence calls and warm-transfer teardown. |
| 23 | Turn-detecting STT + pinned non-external strategies: STT's turn frames no longer drive turns; `ExternalUserTurnStrategies` now interrupts by default unless `enable_interruptions=False` | SILENT | Our Soniox `vad_force_turn_endpoint=False` + pinned strategies; decide explicitly (see §2.4). |
| 24 | `PipelineTask/PipelineParams/PipelineRunner`, `EndTaskFrame/CancelTaskFrame/…`, `FrameProcessor.pipeline_task`, `tool_resources`, `StartFrame.audio_*`, `enable_async_tool_cancellation`, `AzureLLMService(api_version=)`, `pause_watchdog_timeout_s` | DEPRECATED | Warnings only; PEP 702 markers will light up pyrefly. |

### 4.4 Private seams we depend on (all still present in 1.8.1, but re-verify behavior)

| # | Seam | Status in 1.8.1 | Action |
|---|---|---|---|
| 25 | `pipecat.runner.utils._create_telephony_transport`, `parse_telephony_websocket` | present | check signature; transports now connect in `setup()` |
| 26 | `SonioxSTTService._connect_websocket`, `_prepare_language_hints` | present, but body gained `should_interrupt`, `endpoint_sensitivity`, `endpoint_latency_adjustment_level`, `setup()`-time connect and proposed-turn frames | **delete our subclass** (`stt/soniox/service.py`), pass native settings |
| 27 | `GeminiLiveLLMService._process_completed_function_calls`, `_END_FRAME_DEFERRAL_TIMEOUT_SECS`, `_inference_on_context_initialization` | present; the function-call path was rewritten upstream (async payload handling) | our override in `BuddyGeminiLiveLLMService` likely re-introduces the bug it worked around; re-derive from 1.8.1 or drop |
| 28 | `GeminiLLMAdapter._apply_thought_signatures_to_messages`, `_merge_parallel_tool_calls_for_thinking`; `service._adapter` swap | methods present; adapter resolution changed in 1.8.0 (`adapter_class=None`, `get_llm_adapter()`) | verify the `_adapter` attribute name and that thought-signature handling is not already upstream (1.6.0 #4103 grouped parallel tool calls for Gemini thinking) |
| 29 | `AnthropicLLMService._get_llm_invocation_params` (our Vertex/Claude prefill workaround) | present; **1.6.0 upstream appends a `.` user message when context ends on assistant for Claude 4.6+** | our override may now double-append; likely deletable |
| 30 | `BaseOpenAILLMService._get_llm_invocation_params` | **absent** (never existed on the OpenAI base; chat driver calls it only on Anthropic) | fine; OpenAI path uses `build_chat_completion_params` (present) |
| 31 | `GoogleLLMService._build_generation_params`, `_maybe_unset_thinking_budget`, `_tool_config`; `GoogleLLMService.ThinkingConfig` | present (alias to `GoogleThinkingConfig`); default model `gemini-3.6-flash`, `stream_idle_timeout_secs=20` | pin model explicitly |
| 32 | `AzureLLMService.create_client` / `_endpoint` / `_api_version` | present; new `_use_v1_api` branch, `token_provider` | update `_PooledAzureLLMService` to mirror v1 branch |
| 33 | `LLMUserAggregator._user_turn_controller.update_strategies`, `_params.user_mute_strategies`, `_user_is_muted` | present; controllers gained `start()/stop()` | re-test node-level hot-swap |
| 34 | `DailyTransportClient.leave/cleanup/_leave/_cleanup`, `task._pipeline_start_event`, `transport._on_participant_joined` | present; teardown now happens once, transports connect in `setup()` | re-test warm transfer + keepalive |
| 35 | `TwilioFrameSerializer._hangup_attempted` via `transport.output()._params.serializer` | present | ok |
| 36 | `MCPClient._tool_wrapper` | present; `register_tools()` deprecated → `tools()` | ok |
| 37 | `RTVIProcessor` attached by `PipelineTask(enable_rtvi=…)`; RTVI protocol 2.0/2.1 (`bot-output` spoken progress, `dtmf.buttons`) | present | widget client SDK must accept 2.x |

### 4.5 Things that are fine

Deepgram `LiveOptions`/`live_options` still accepted; `OpenAISTTService(language, prompt, temperature)` unchanged; `CartesiaTTSService`/`GenerationConfig` unchanged (`cartesia_version` deprecated, header auth); `SarvamTTSService.Settings(pace, pitch, loudness, enable_preprocessing)` unchanged; `SmartTurnParams`, `LocalSmartTurnAnalyzerV3(cpu_count=…)`, all turn-strategy classes, `AlwaysUserMuteStrategy`, `WakePhraseUserTurnStartStrategy`, `SoundfileMixer`, `DailyRunnerArguments`, `GoogleVertexLLMSettings`, `AnthropicLLMSettings.enable_prompt_caching`, `TTSService(push_start_frame, push_stop_frames)`, `run_tts(text, context_id)`, `TextAggregationMetricsData`, `LLMUserAggregatorParams(user_idle_timeout)` + `on_user_turn_idle` all present. `dragontts/` does not import pipecat and is unaffected.

---

## 5. Suggested sequencing

1. **Mechanical upgrade branch** — bump to `pipecat-ai[...]==1.8.1`, remove `pipecat-ai-flows`, `uv lock`; fix #1–#3; delete the Soniox subclass; run import-smoke of routers/registries/contracts, `pyrefly`, `pytest`.
2. **Behavior audit** — #16 (every `TTSSpeakFrame`), #17 (handler cancellation), #18/#19 (timeouts + unusable policy), #23 (turn-strategy decision), #27–#29 (re-derive or delete overrides), dashboards (#20–#21). Ship with `processor_unusable_policy=END` and `UserBotLatencyObserver` + `StartupTimingObserver` attached in prod.
3. **Latency pass** (needs 2): VAD on, smart-turn v3.2 or Soniox external strategies, `retry_on_timeout`, HTTP/2 pool in the voice subprocess, prompt caching, TOKEN mode A/B for flash, Azure Global-Standard deployment on a priority-eligible model with `service_tier="priority"` and a smoothed dispatch ramp.
4. **TTS/STT pilots**: ElevenLabs v3 Conversational via `ElevenLabsDialogueTTSService` behind DragonTTS on expressive nodes only; Sarvam bulbul v3/v4 and saaras:v4 realtime on a Hindi cohort; `stt-benchmark` on our recordings to pick the Hindi STT by measured TTFS P99 and WER.
5. Retire `docs/pipecat-upgrade-recommendations.md` (stale) in favor of this document once step 1 lands.

---

## Sources

- pipecat CHANGELOG (`v1.2.0`…`v1.8.1`) and source tree at `v1.8.1`; pipecat-flows CHANGELOG 1.1.0–1.4.0
- ElevenLabs: Models overview; Text-to-Dialogue realtime websocket guide; Eleven v3 blog
- pipecat docs: ElevenLabs TTS service reference; STT latency tuning; `stt-benchmark`; smart-turn README (v3.2, 23 languages incl. Hindi/Marathi)
- Microsoft Learn: Enable priority processing for Microsoft Foundry Models (2026-07-13)
- pipecat-ai/nemotron-january-2026 streaming pipeline architecture; FutureAGI "12 techniques" (2026)
- Sarvam bulbul v3 blind-study claims and Cartesia sonic-3 latency figures are vendor/third-party marketing numbers; verify on our own audio before deciding.
