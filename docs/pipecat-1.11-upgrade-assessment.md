# Pipecat 1.1.0 → 1.11.0: upgrade assessment for the Breeze Buddy cascading pipeline

**Date:** 2026-09-18 (supersedes the 2026-09-03 pass against 1.8.1)
**Baseline:** `release` @ `1189023e`, `pipecat-ai==1.1.0` (2026-04-27), `pipecat-ai-flows==1.0.0`
**Target:** `pipecat-ai==1.11.0` (tag `v1.11.0`, 2026-09-17), Flows bundled as `pipecat.flows`
**Method:** full read of the pipecat CHANGELOG span 1.2.0–1.11.0 (≈7,600 lines) and the Flows changelog 1.1.0–1.4.0, an AST-level inventory of every pipecat symbol this repo touches, a source-level check of every private seam we depend on against the `v1.11.0` tree, and a re-read of the 50 commits that landed on `release` since the previous pass.

This document supersedes `docs/pipecat-upgrade-recommendations.md` (written against 0.0.101–0.0.105; several of its file references no longer exist).

---

## 0. Where we stand

| | Ours | Latest | Delta |
|---|---|---|---|
| pipecat-ai | 1.1.0 | 1.11.0 | 10 minor releases in 5 months |
| pipecat-ai-flows | 1.0.0 | 1.4.0 (**final, frozen**) | Flows merged into `pipecat.flows` at pipecat 1.5.0; the standalone package pins `pipecat-ai<1.5` and cannot coexist |
| openai | 2.20.0 | `>=1.74,<4` | a fresh resolve picks **openai 3 on httpx2** (1.10.0); see §5.1 |
| anthropic | 0.49.0 | `>=0.49,<2` | anthropic 1 SDK allowed (1.10.0) |
| google-genai | 1.73.1 | `>=2.19.0` for the `google` extra | floor raised (1.11.0) |
| aic-sdk | 2.2.0 | `~=3.1.0` | breaking SDK port (1.8.0) |
| daily-python | 0.28.0 | `>=0.29.0` | bump |
| mcp | — | `>=1.24.0,<3` | floor raised (1.10.0) |
| Python | 3.11 | `>=3.11` | fine |

**How the pipeline is built today** (`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`):

```
transport.input() → stt → TranscriptionGate → [KB] → user_aggregator → llm → tts → MetricsCollector → transport.output() → assistant_aggregator
```

- Telephony over `FastAPIWebsocketParams` at 8 kHz; serializer built by pipecat's private `_create_telephony_transport`. Daily bots now fork from a pre-imported zygote (`services/daily/zygote.py`).
- STT default Soniox, `vad_force_turn_endpoint=False`, `language_hints="en,hi"`, our subclass now also carries an **endpoint watchdog** (`finalize_after_secs`), tighter websocket ping/close timeouts, and a flush-on-disconnect (the 2026-09-16 "no `<end>` token, 35–50 s dead air" incident).
- **Silero VAD is off in production** (`BREEZE_BUDDY_ENABLE_VAD=False`), so turn start is transcription-driven and pipecat's `UserBotLatencyObserver` is deliberately not attached.
- Turn stop: `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)` or `TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3)`.
- LLM: `AzureLLMService` (`service_tier="auto"`, `max_completion_tokens=50`, `function_call_timeout_secs=10`, optional `tool_choice="required"` and `extra_body` for gateways). No `retry_on_timeout`. HTTP/2 pool only in chat mode.
- **Tool-based speech** (`agent/early_speech.py`, `docs/TOOL_BASED_SPEECH_ARCHITECTURE.md`): say-tool lines are queued to TTS the moment the streamed function *name* decodes, by wrapping `get_chat_completions` and `process_frame` on the LLM service instance.
- **Turn-1 prompt-cache prefill** (`agent/prompt_prefill.py`): one non-streaming request with the exact prefix during greeting playback.
- TTS default ElevenLabs `eleven_flash_v2_5` over the India-residency socket; DragonTTS caching proxy in front when enabled.
- Flows: `FlowManager(task=…)`, `FlowsFunctionSchema` handlers returning `(result, NodeConfig|None)`, `respond_immediately` now set per node, extra keys stuffed into `NodeConfig`.
- Metrics: `MetricsCollectorProcessor` reads `MetricsFrame` (TTFB, processing, text aggregation) per turn and the result is written to `lead_calls.meta_data.pipecat_metrics` at end of call. Nothing in the repo reads it back. See §6.

---

## 1. What landed on `release` since the last pass, and how it sits against 1.11

| Our change (commit) | What it does | Against pipecat 1.11 |
|---|---|---|
| `feat: tool based conversation architecture` (`f473b6d2`) — `agent/early_speech.py`, `template/transition.py`, `tool_choice` | Speaks a say-tool's fixed line at function-name decode; handler speaks the dynamic argument; empty `{}` result stops the LLM re-run | **Survives.** `get_chat_completions` is still the stream entry (`base_llm.py:448`), the chunk shape is unchanged, `FrameProcessor.queue_frame` and `LLMAssistantPushAggregationFrame` exist, and the aggregator still gates the re-run on a truthy result (`if frame.result:`), so the `{}` contract holds. Native equivalent now exists: return `(result, NO_RESPONSE)` (`pipecat.flows.NO_RESPONSE`, Flows 1.3 / pipecat 1.6). The `TTSSpeakFrame(append_to_context=True)` is already explicit, so the 1.4.0 default flip does not affect it. Re-test the forced `LLMAssistantPushAggregationFrame`: since 1.4.0 `TTSStartedFrame.append_to_context` opens the assistant turn itself, so the push may now be redundant (harmless, but verify no double commit). Note the docstring cites `tests/test_early_speech.py`, which is not in the tree. |
| `(feat): add a warming llm request…` (`7ab45708`) — `agent/prompt_prefill.py` | Turn-1 cache prefill via `service._client`, `service._settings`, `get_llm_adapter().get_llm_invocation_params(system_instruction=, convert_developer_to_user=)`, `build_chat_completion_params`, `pipecat_flows.types.FlowsDirectFunctionWrapper`, `NOT_GIVEN` from `llm_context` | **Survives with two edits.** All seams present in 1.11; `NOT_GIVEN` is still re-exported from `llm_context`; `FlowsDirectFunctionWrapper` and `to_function_schema` live in `pipecat.flows.types`. Change the import root, and note `supports_developer_role` is now a plain class attribute (assignment still works). On the Azure `/openai/v1` surface `_client` is the v1 client; params are the same. 1.9.0 adds `LLMTokenUsage.cache_creation_input_tokens` for OpenAI-compatible services, so the prefill's `cache_write` log can move off the private `prompt_tokens_details` read. |
| `fix: soniox not sending final transcript` (`ece566b4`) — `stt/soniox/service.py` | Watchdog sends `{"type":"finalize"}` after N s of un-endpointed finals; ping 3 s / pong 5 s / close 3 s; flush buffered finals on disconnect; per-connection stats log | **Still needed, but shrink it.** 1.11 sends `max_endpoint_delay_ms`, `endpoint_sensitivity`, `endpoint_latency_adjustment_level` natively (drop the shim). 1.11 still has no idle-finalize watchdog and no ping tuning, and `_disconnect_websocket` still does not flush finals, so those three overrides stay. Adjust: call `self._websocket_connect(self._url, ping_interval=…, ping_timeout=…)` instead of `websocket_connect` directly (the helper injects `ws_close_timeout` and the bounded-close connection class, 1.7.0); call `emit_stt_usage_metrics()` before the rescued `TranscriptionFrame` like upstream does; upstream `_connect_websocket` now swallows the connect exception (sets `_websocket=None`) whereas ours re-raises, which under 1.8+ marks the service permanently unusable during `setup()`. `_final_transcription_buffer`, `_last_tokens_received`, `FINALIZE_MESSAGE`, `_handle_transcription(transcript, is_final, language)` all present. 1.9.0 #5682 fixes the unclean-close/double `on_disconnected` on graceful end. |
| `feat(daily): fork voice bots from a pre-imported zygote` (`c1eca642`) | Imports the agent once, forks per call | **Compatible, and better on 1.11.** pipecat 1.8.0 halved import time and made NLTK lazy; 1.9.0's `StartupTimingReport.warmup` measures exactly the deferred-import cost the zygote amortises. No event loop or ONNX session is created at import. Keep the "refuse to fork with a running loop / open pool" guards. |
| `tool_choice`, `extra_body`, `disable_thinking`, `supports_developer_role=False` on the OpenAI path | Gateway tuning | `settings.extra` is still merged last in `build_chat_completion_params` (`base_llm.py:351-386`), so `tool_choice`, `reasoning_effort`, `extra_body` pass through unchanged. |
| `respond_immediately` on `NodeConfig` | Closing node opts out of the LLM run | Real flows key, honoured in 1.11 (`manager.py:783`). |
| `finalize_after_secs` in `SonioxSTTConfig`, `retry_until` on HTTP requests, `response_reveal`, chameleon UI | Template surface | Not pipecat-coupled. |

---

## 2. New features we can actually use (mapped to our code)

Grouped by pipeline stage. Version in brackets. Items new since the 1.8.1 pass are marked **(new)**.

### 2.1 Turn-taking and interruptions

| Feature | Version | Why it matters for us | Where it lands |
|---|---|---|---|
| **Eager end-of-turn speculative inference (new)** — `enable_eager_end_of_turn=True` on `DeepgramFluxSTTService` / `CartesiaTurnsSTTService`; the service recommends `EagerUserTurnStrategies`; the LLM starts on the predicted end and the response is held by a `SpeculationGate` inside the LLM service until the turn commits; discarded if the user resumes or the committed transcript differs (`NormalizedMatch` / `ExactMatch`); `speculation_timeout=5.0` | 1.9.0 | This is the real "generate during the endpoint gap" mechanism. **Flux Multilingual supports Hindi** and pipecat maps `Language.HI` for it; Cartesia Ink-2 is English-only. Not available for Soniox, Sarvam or AssemblyAI. The plumbing (`UserTurnSpeculation`, `trigger_user_turn_inference_triggered(speculation=…)`, `LLMContextFrame(speculation=True)`) is generic, so a custom stop strategy could speculate off Soniox partials. See §3.4. | `pipeline.py:376-461`, STT router |
| Proposed turn frames + `ExternalUserTurnStrategies`; the 0.5 s per-turn delay fix for exactly our Soniox configuration | 1.5.0, 1.8.0 | Lets Soniox's endpointer close the turn. With pinned VAD/transcription strategies the STT's turn frames are now *ignored* (1.8.0). | `pipeline.py:376-461` |
| Soniox `endpoint_sensitivity` (v5), `max_endpoint_delay_ms`, `endpoint_latency_adjustment_level`, `should_interrupt` as native `Settings` | 1.3.0–1.5.0 | Removes our `max_endpoint_delay_ms` shim. | `stt/soniox/service.py` |
| `LocalSmartTurnAnalyzerV3`: no `transformers`, 60 MB / 0.3 s cold start, absolute-deadline safety net, release-on-verdict (`wait_for_transcript=False`) | 1.3.0, 1.7.0 | Smart-turn v3.2 covers Hindi and Marathi, 10–100 ms on CPU. With the zygote, the model can be loaded once per pod. | `pipeline.py:340-371` |
| LLM turn-completion markers `●/◐/○` (single tokens), `FilterIncompleteUserTurnStrategies`, **rewritten default instructions aimed at weaker models such as gpt-4.1-mini (new)**, `LLMMarkerResponseFrame` and RTVI `bot-llm-marker` for observing marker accuracy (new) | 1.2.0, 1.8.0, 1.11.0 | The second-judge mechanism. Default prompts are English; supply Hindi `UserTurnCompletionConfig` prompts. | new |
| `AudioVolumeTracker` (400 ms window) behind `VADParams.min_volume` | 1.8.0 | Fewer false speech stops on soft Hindi phonemes; makes turning VAD back on safer. | `template/vad.py` |
| `idle_timeout_frames` now counts `TranscriptionFrame` / `InterimTranscriptionFrame` / `UserStartedSpeakingFrame` as activity **(new)** | 1.9.0 | Our VAD-off pipeline could previously be idle-cancelled mid-utterance. | free |
| Fixes: delayed interruptions, TTS deadlock with `pause_frame_processing`, phantom end-of-turn from stale smart-turn state, one-LLM-call-per-fragment, idle re-prompt with `run_llm=True` swallowed, `UserIdleTimeoutUpdateFrame` applied immediately | 1.2.0–1.7.0 | Free on upgrade; the idle fix affects our `UserIdleCallbackHandler` ladder. | — |
| `FunctionCallUserMuteStrategy`, `MuteUntilFirstBotCompleteUserMuteStrategy` | ≤1.8.0 | Alternatives to ad-hoc `mute_stt()` during `_speak_and_wait`. | `template/interruption.py` |
| Function-call timeouts no longer disarmed by an intermediate `is_final=False` update **(new)**; async tools with a `TTSSpeakFrame` filler settle as ordinary results **(new)** | 1.11.0 | Matters for say-tool fillers plus async global functions. | `mcp/`, `global_function.py` |

### 2.2 STT

| Feature | Version | Notes |
|---|---|---|
| Sarvam transcripts now `finalized=True` **(new)**; no longer mislabelled as Hindi when language undetected **(new)**; `saaras:v4` default; `SarvamRealtimeSTTService` (`saaras:v3-realtime`, `endpointing="vad"|"manual"`, accepts `saaras:v4`) | 1.8.0–1.11.0 | The `finalized` fix removes a full turn-analyzer timeout per turn on Sarvam. **`saaras:v2.5`/`saarika:v2.5` and the `prompt` setting are gone** (§5). |
| Soniox `stt-rt-v5` default, majority-token language labelling for code-mixed utterances, end-of-audio close fix **(new)** | 1.2.0–1.9.0 | Hinglish: an utterance ending in English is no longer tagged `en`. |
| Azure STT `finalized=True` fast path; `profanity="raw"`; `segmentation_silence_timeout_ms` **(new)** | 1.4.0, 1.9.0 | If Azure STT is used for Hindi: set all three. |
| Deepgram: first-word fix, 3-strike reconnect, sdk 6/7, `version` pin, **`profanity_filter` no longer sent (new, default off)** | 1.3.0–1.9.0 | nova-3 handles Hindi code-switching. |
| Deepgram Flux Multilingual (10 languages incl. Hindi, model-based end-of-turn <400 ms, eager end-of-turn) **(new in scope)** | 1.9.0 | The only Hindi-capable STT that can drive speculative inference. Benchmark against Soniox on our recordings. |
| AssemblyAI `universal-3-6-pro` **(new)**, `language_codes` steering (now incl. Marathi) **(new)**, `AssemblyAISyncSTTService` **(new)**, context carry-over, `voice_focus` | 1.4.0–1.11.0 | Hindi is not a tier-1 steering code; Indian-English option only. |
| `GeminiSTTService`, `GoogleSTTService` phrase adaptation + `denoiser_config` **(new)** | 1.8.0, 1.9.0 | Hindi candidates to benchmark. |
| STT connect failures → non-fatal `ErrorFrame` + `ServiceSwitcher` failover; `is_usable` model | 1.3.0, 1.8.0 | STT fallback without dropping the call. |
| `STTUsageMetricsData.audio_seconds`, `supports_ttfs`, `ttfs_p99_latency` override | 1.3.0, 1.7.0 | Cost + TTFS accounting per provider. |
| STT services transcribe only `InputAudioRawFrame` **(new)** | 1.9.0 | Injected `OutputAudioRawFrame`s (our greeting path) no longer reach the STT provider. |

### 2.3 TTS

| Feature | Version | Notes |
|---|---|---|
| `ElevenLabsDialogueTTSService` for `eleven_v3` / `eleven_v3_conversational`; 1008-close fix on a quiet context **(new)** | 1.8.0, 1.11.0 | See §4. |
| ElevenLabs: `close_context` at turn completion; **`alignment` instead of `normalizedAlignment` so Devanagari is not written to context transliterated**; keepalive race fix; default model now `eleven_flash_v2_5` | 1.2.0–1.7.0 | The Devanagari fix is the single most important Hindi item in the span. |
| `TextAggregationMode.TOKEN` made correct (sentence regrouping, per-context sequencer, stray-timestamp handling **(new)**) | 1.6.0–1.11.0 | Our `BB_AGGREGATE_SENTENCES=false` path is safe to A/B. |
| **Output transport dropped speech when TTS paused >200 ms between chunks and TTS rate ≠ wire rate (new fix)** | 1.11.0 | Exactly our 8 kHz telephony case with 16/24 kHz TTS. |
| Sarvam TTS `TTSStoppedFrame` on `final`; dropped-first-audio fix; `bulbul:v3` / `shubh` / 24 kHz defaults and `SarvamHttpTTSService` sample-rate fix **(new)** | 1.2.0–1.9.0 | Kills dead air and clipped first syllables. We already pin `bulbul:v3`; confirm `shreya` exists on v3. |
| Azure TTS `force_locale`, `private_endpoint`, `voice_parameters` (HD voices) **(new)**, **`effect="eq_telecomhp8k"` telephony EQ (new)** | 1.3.0–1.11.0 | `force_locale` stops accent flips on Hinglish multilingual voices. |
| `text_transforms` (`VoiceFormatter`, `replace_text`) with original text kept in context | 1.5.0 | English-only expansions; use `replace_text` for pronunciation, never number/date expansion on Hindi. |
| TTFA metrics (`ttfa`, `ttfb`, `leading_silence`); `max_consecutive_zero_audio_contexts`; services pre-connect during `setup()` | 1.5.0, 1.8.0 | See §6. |
| Cartesia default `sonic-3.6` **(new)**; Odia/Urdu mapped **(new)** | 1.9.0 | We pin `sonic-3.5`. |

### 2.4 LLM

| Feature | Version | Notes |
|---|---|---|
| `AzureLLMService` `/openai/v1` surface, `token_provider` (Entra ID), `api_version` deprecated | 1.8.0 | New Azure features only land on v1; `_PooledAzureLLMService.create_client` must mirror the `_use_v1_api` branch. |
| `service_tier` first-class (we pass `"auto"`); `retry_on_timeout` / `retry_timeout_secs` (default 5.0) | ≤1.1.0 | One-word change to `"priority"` once the deployment qualifies (§3.3). Retry never enabled. |
| `LLMTokenUsage.cache_creation_input_tokens` from OpenAI-compatible services incl. Azure **(new)** | 1.9.0 | Lets the prefill and the metrics collector report cache writes vs reads. |
| `LLMSwitcher` + `ServiceSwitcherStrategyFailover` (switch only on `is_usable=False`), `reach_inactive_services`, Gemini placeholder `thought_signature` for foreign tool calls **(new)**, Gemini "." user-turn append when context ends on assistant **(new)** | 1.7.0–1.11.0 | Azure → Gemini failover mid-conversation with prior tool calls and say-tool fillers now works. |
| `system_instruction` on the service; **`"system"` message at the start of `LLMContext` deprecated, removed in 2.0 (new)**; Flows `role_messages` deprecated in favour of `role_message` → `system_instruction` | 1.3.0, 1.9.0 | Our `template/builder.py:401` builds `{"role":"system"}` role messages; chat mode prepends system blocks (`chat/agent/context.py:177`, `approval.py:416`). Warnings now, breakage at 2.0. Prompt-cache note: moving the prompt to `system_instruction` changes the serialized prefix once; the prefill assembles through the same path so parity holds. |
| `add_tool_change_messages=True`; `@tool_options(cancel_on_interruption, timeout_secs)`; `cancellable_by_llm`; `FunctionCallResultFrame.error` **(new)** | 1.2.0–1.9.0 | Per-tool timeouts and honest failure recording for flow handlers. |
| Function-call timeout now **cancels** the handler | 1.8.0 | Behaviour change (§5.3). |
| `TTFATMetricsData`, unified TTFB definition | 1.8.0 | Azure vs Gemini vs Claude comparable (§6). |
| anthropic 1 SDK (`temperature`/`top_k`/`top_p` via `extra_body`) **(new)**; OpenAI Responses `service_tier="fast"` documented **(new)** | 1.10.0, 1.9.0 | Responses-only for `fast`; Azure priority is set at the deployment or via `service_tier="priority"` on chat completions. |

### 2.5 Transport / telephony

| Feature | Version | Notes |
|---|---|---|
| `FastAPIWebsocketParams.ws_close_timeout` (0.5 s) — fixes ~10 s teardown stall on half-closed sockets | 1.4.0 | Frees pods sooner. |
| **Websocket transport pacing fix (new):** frames the serializer resampled and emitted later were treated as unsent, so on 8 kHz wire vs 16/24 kHz TTS the output queue drained instantly and `BotStoppedSpeakingFrame` / `EndFrame` arrived early. "Matters most where the serializer hangs up the call on the `EndFrame`" | 1.10.0 | Twilio/Plivo/Exotel hangup timing and every "bot stopped speaking" consumer (our metrics turn commit, idle timer, mute strategies) shift after upgrade. |
| `resampler_clear_after_secs` on telephony serializers (0.2 s); `BaseAudioResampler.flush()/reset()` **(new)** | 1.5.0, 1.11.0 | Verify no artefacts on long silences. |
| `TransportParams.audio_out_write_timeout_secs` (10 s) → transport unusable | 1.8.0 | Interacts with our `NonClosingWebSocket` proxy; needs a `processor_unusable_policy`. |
| `SingleClientWebsocketServerTransport` drains farewell audio before closing | 1.6.0 | Goodbye lines no longer cut. |

### 2.6 Pipeline / infra / observability

| Feature | Version | Notes |
|---|---|---|
| `PipelineWorker` (was `PipelineTask`), `WorkerRunner`, `pipecat.workers` bus; `setup_timeout_secs`/`start_timeout_secs` (20 s), `processor_unusable_policy` | 1.3.0, 1.8.0 | Aliases keep our code running; defaults are new failure modes (§5.3). |
| `PipelineFlushFrame`, `pause_processing_all_frames_until()`, `broadcast_interruption()` | 1.4.0, 1.8.0 | For `_speak_and_wait` and transfer flows. |
| **New observers (new):** `ServiceMetricsObserver`, `SpeakingObserver`, `ErrorObserver`, `FunctionCallObserver`; `UserBotLatencyObserver.on_latency_breakdown` with `contributions` that sum to the measured latency; `StartupTimingReport.warmup` | 1.9.0 | The basis of §6. |
| OTEL fixes: STT/TTS `metrics.ttfb` corrected and parented to the turn span; LLM `output` on interrupted turns; TTS span text restored; `gen_ai.provider.name=azure.ai.openai`; audio-token usage | 1.2.0, 1.6.0 | **Pre-1.2 TTS/STT TTFB numbers in Langfuse were wrong.** Re-baseline. |
| `pipecat.evals` (`pipecat eval`, latency budgets, LLM-judged criteria, simulations **(new)**) | 1.4.0+ | Text-mode scenarios can regression-test Hindi flows. |
| Flows: `NO_RESPONSE`, `@flows_tool_options`, `append_text_to_context` on `tts_say`/`end_conversation`, `FlowConfig` YAML/JSON flows with `TRANSITION_IN_YAML` **(new)** | Flows 1.1–1.4, 1.9.0 | See §5.2 for the behaviour changes. |

---

## 3. Sub-second latency with a cascading pipeline (and no PTU)

### 3.1 What the field is doing (last ~6 months)

- **Pipelining, not sequencing.** The pipecat/NVIDIA Nemotron reference lands 500–700 ms voice-to-voice on a cascade by streaming every stage, emitting the LLM's first segment at a sentence boundary capped at ~24 tokens, and using streaming TTS for the first segment only.
- **Turn-end is the biggest lever, not the LLM.** Pipecat models responsiveness as `user stops → [TTFS] → final transcript → bot starts` and ships `stt-benchmark` for P50/P90/P99 TTFS per provider. Model-based endpointing (smart-turn v3.2, Deepgram Flux, Cartesia Ink-2, Soniox v5 sensitivity) replaces silence timeouts.
- **Speculative inference, now in the framework.** pipecat 1.9.0 answers an STT's *eager* end-of-turn prediction while the turn is still open and holds the response until the committed transcript matches. Separately, the LLM-judge marker path (`●/◐/○`) lets an early detector trigger inference while the LLM itself decides whether the user was done.
- **Prompt-prefix caching + stable context** (80–200 ms TTFT and ~50 % input cost on Azure Standard). We shipped the turn-1 prefill.
- **Priority processing / "Fast mode"** on Azure OpenAI: pay-as-you-go tier with p50 latency targets and 99 % > 80–100 TPS commitments. The no-PTU answer to inconsistent latency; details in §3.3.
- **Say what you can before the LLM finishes.** Our say-tool early fire is the strongest version of the "filler on tool call" pattern: the line is authored, cached, and starts at name-decode.
- **Warm everything**: sockets, VAD/ONNX models, TTS contexts, HTTP/2 pools, and now the interpreter itself (zygote). pipecat 1.8.0 connects services concurrently during `setup()`; 1.9.0's `StartupTimingReport.warmup` shows what the zygote saves.

### 3.2 Where a turn's budget goes today and what changes it

| Stage | Today (est.) | Lever | After |
|---|---|---|---|
| Speech end → turn end | VAD off; wait for Soniox final (`max_endpoint_delay_ms=500`, watchdog at 1 s); pinned strategies ignore Soniox's own turn frames on 1.8+ | VAD on (robust `min_volume`), `ExternalUserTurnStrategies` or smart-turn v3.2 with `wait_for_transcript=False`, Soniox `endpoint_sensitivity` | 150–350 ms |
| STT final transcript | Soniox TTFS (measure) | `stt-benchmark` on our Hindi recordings: Soniox v5 vs Flux Multilingual vs Sarvam saaras:v4 | provider-dependent |
| LLM TTFT | Azure Standard, spiky 500–1,900 ms | priority tier, prefill (shipped), `retry_on_timeout`, HTTP/2 pool in the voice subprocess, failover | 250–450 ms p95 |
| Tool-call turns | second LLM call after the transition | say tools (shipped): line starts at name-decode, no second call for fixed lines | −400 to −1,500 ms |
| Sentence aggregation | wait for first full sentence | TOKEN mode for ElevenLabs flash (now correct) or prompt-cap the first sentence | −100 to −300 ms |
| TTS TTFB | flash v2.5 ~75 ms + network; v3 conversational ~280 ms | keep flash / Sarvam for the first segment; DragonTTS hit ≈1 ms for fixed lines | 100–250 ms |
| Output | 8 kHz μ-law | 1.10/1.11 transport fixes (pacing, 200 ms gap) | — |

### 3.3 Azure without PTU

1. **Priority processing** — `service_tier="priority"` on the request (we already pass `service_tier`) or at the deployment (`"properties": {"service_tier": "priority"}`). Requirements (Microsoft Learn, 2026-07-13): **Global Standard** or **Data Zone Standard (US)** deployment; model versions 2025-12-01+ plus **gpt-4.1 (2025-04-14)**; supported today: gpt-4.1, gpt-5.1, gpt-5.2, gpt-5.4, **gpt-5.4-mini** (99 % > 100 TPS target), gpt-5.5, gpt-5.6. **gpt-4o and gpt-4.1-mini are not eligible**, so this is a model move. Same quota as standard; billed at the priority rate only when served as priority (`service_tier` echoed in the response — log it). **Ramp-rate limit: >50 % TPM growth within 15 min is downgraded silently**; smooth campaign starts. `southindia` is in the Global Standard list.
2. **Prompt caching** — shipped as the turn-1 prefill; verify with `cache_read_input_tokens` and, from 1.9.0, `cache_creation_input_tokens` (§6).
3. **`retry_on_timeout=True, retry_timeout_secs≈2.0`** on the voice `AzureLLMService`.
4. **HTTP/2 pooled client in the voice subprocess** (today chat only); with the zygote, pre-warm once per pod. Beware openai 3 / httpx2 (§5.1).
5. **Failover** with `LLMSwitcher` + `ServiceSwitcherStrategyFailover`: Azure primary, OpenAI-direct or `gemini-3.6-flash` secondary. Switches on `is_usable=False`, not on slowness; slowness is handled by 3. The 1.11 `thought_signature` placeholder makes switching into Gemini after Azure tool calls work.
6. **Measure honestly** — §6.

### 3.4 Speculative inference, concretely, with pipecat 1.11

Two mechanisms now exist. They answer different questions and can be combined.

**A. STT-driven eager end of turn (1.9.0).** `DeepgramFluxSTTService(enable_eager_end_of_turn=True, settings=Settings(eager_eot_threshold=…))` plus the strategies it recommends (`EagerUserTurnStrategies`). Flux predicts the end, pipecat runs the LLM on a provisional context (`LLMContextFrame(speculation=True)`), the LLM service's `SpeculationGate` holds text/audio, and the committed transcript either releases (`confirms_speculation=True`, no second inference) or discards (`EagerEndOfTurnCancelFrame`, inference re-runs on the committed text). Nothing unconfirmed reaches the user, the context, or a tool handler. Requires **Flux Multilingual** for Hindi; it replaces the detector chain, so it cannot run alongside smart-turn. Cost: wasted Azure calls at the miss rate, bounded by `match_policy` and `speculation_timeout`.

**B. LLM-judge markers (1.2.0, improved 1.8.0/1.11.0).** An early detector wrapped in `deferred(...)` triggers inference; the LLM starts every response with `●/◐/○`; only `●` finalizes. Works with any STT including Soniox. The 1.11 rewrite of the default instructions targets smaller models such as gpt-4.1-mini. Cost: one extra token per response plus a prompt block; Hindi prompts needed.

```
user_turn_strategies=UserTurnStrategies(
    start=[VADUserTurnStartStrategy(), MinWordsUserTurnStartStrategy(min_words=…)],
    stop=[
        deferred(TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(...), wait_for_transcript=False)),
        LLMTurnCompletionUserTurnStopStrategy(config=UserTurnCompletionConfig(...)),
    ],
)
```

**What it is not.** Neither mechanism starts the LLM on interim text mid-speech. For a clean one-sentence turn ("I don't need the offer") the gain is bounded by how early the detector fires versus the final endpoint; the wins on that utterance are the say-tool fire, priority tier, and turn-end tuning, not speculation. Speculation pays on trailing-off and on long turns.

---

## 4. TTS: ElevenLabs v3 Conversational, Hindi/Hinglish, and what pipecat now handles

### 4.1 Eleven v3 Conversational facts (ElevenLabs docs, Sept 2026)

| | `eleven_flash_v2_5` (current) | `eleven_v3_conversational` | `eleven_v3` |
|---|---|---|---|
| Model latency | ~75 ms | **~280 ms** (excl. network) | higher; not real-time |
| Languages | 32 (Hindi yes) | 70+ (Hindi yes) | 70+ |
| Transport | TTS websocket (`chunk_length_schedule`, `auto_mode`) | **Text-to-Dialogue websocket** | same |
| First-audio threshold | configurable | **fixed ≈ 40 chars / 8 words**; `flush` to force | same |
| Voice settings | stability, similarity, style, speed, speaker boost | **`stability` only** | same |
| Audio tags | no | yes | yes |
| Voices per connection | 1 | exactly 1 | up to 10 |
| Timestamps | alignment | `sync_alignment=true` | same |
| Idle timeout | — | 20 s keep-alive | same |
| Concurrency | standard pool | dedicated pool | same |

pipecat ships `ElevenLabsDialogueTTSService` (1.8.0; 1.11.0 fixes a 1008 close when a quiet context was dropped mid-generation). It forces `TextAggregationMode.SENTENCES`, resolves `pcm_8000/16000/24000` from the pipeline rate, and reads only `stability`. The changelog says `ElevenLabsTTSService` with Flash stays lower-latency.

### 4.2 Verdict

- **Do not switch the default to v3 Conversational for latency.** Roughly +200–300 ms TTFB before network, and the 40-char / 8-word server buffer is hostile to short Hindi replies.
- **Pilot it for expressiveness** on greeting and empathy nodes, behind DragonTTS and failover to flash. Say-tool lines are fixed text, so v3 audio for them is a cache hit after the first call regardless of model latency.
- **Benchmark Sarvam `bulbul:v3` first** on the same cohort: third-party blind tests prefer it for Hindi at telephony 8 kHz and it handles code-mixing in one pass; on upgrade it gets the stop-frame, dropped-first-audio and sample-rate fixes.
- **Measure with TTFA, not TTFB.** `leading_silence` separates model latency from silence padding.

### 4.3 Hindi / Hinglish handled by pipecat (no local fixes)

ElevenLabs `alignment` for Devanagari context (1.2.0); Soniox majority-token language labelling (1.2.0); Sarvam not defaulting to Hindi labels (1.9.0); Azure TTS `force_locale` (1.7.0); Azure STT `profanity="raw"` (1.4.0); word-timestamp tracking robust to curly quotes/dashes/tags (1.8.0–1.11.0); `Language.EN_IN` resolves to `"en"` with a warning on ElevenLabs (base-code fallback via `resolve_language`), so the four `EN_IN` call sites keep working.

### 4.4 Local fixes we keep, and ones that become redundant

| Ours | After upgrade |
|---|---|
| `EmojiTextFilter` | keep. `max_consecutive_zero_audio_contexts=3` (1.8.0): three consecutive fully-stripped turns mark the TTS unusable; set to 0 or handle empties before the TTS. |
| DragonTTS proxy | keep; `start()` → `setup()`; it forwards no word timestamps, so the 1.9.0 `TTSTextFrame` semantics change does not touch it. |
| ElevenLabs India-residency URL | keep. |
| `SonioxSTTServiceWithEndpointDelay` | shrink to watchdog + ping + flush (§1). |
| Say-tool early fire | keep; consider `NO_RESPONSE` and a check of the forced push (§1). |
| Pronunciation fixes | `text_transforms=[("*", replace_text(...))]`, not pronunciation dictionaries (deprecated 1.6.0). |
| Number/date reading in Hindi | do not enable `VoiceFormatter` (English output). |

---

## 5. What stops working when we pull 1.11.0 (verified against source)

Legend: **HARD** = import or construction error; **SILENT** = runs but behaves differently; **PRIVATE** = private seam still present but changed; **DEPRECATED** = warning now, removal at 2.0.

### 5.1 Dependencies and imports

| # | Item | Class | Where |
|---|---|---|---|
| 1 | `pipecat-ai-flows` cannot coexist with pipecat ≥1.5; drop it and change `pipecat_flows` → `pipecat.flows` (13 files incl. `prompt_prefill.py`); all symbols present (`FlowManager, FlowsDirectFunction, FlowsFunctionSchema, NodeConfig, ActionConfig, FlowResult, FlowsDirectFunctionWrapper`) | HARD | pyproject, `agent/flow.py`, `agent/prompt_prefill.py`, `template/builder.py`, chat, mcp |
| 2 | `SarvamSTTService.Settings(prompt=…)` — **confirmed gone**: `prompt` exists only on `SarvamRealtimeSTTSettings`; `saaras:v2.5`/`saarika:v2.5` removed; default `saaras:v4` | HARD | `app/ai/voice/stt/sarvam.py:89-102` |
| 3 | `aic-sdk` 2.2 → ~=3.1: `AICFilter(license_key, model_id|model_path, model_download_dir, enhancement_level)`; env `AIC_LICENSE_KEY` → `AIC_SDK_LICENSE`; energy VAD removed | HARD (SDK) | `agent/transport.py:35-56` |
| 4 | **openai 3 on httpx2 (new)**: pipecat allows `<4`, so a fresh lock picks openai 3, whose clients use `httpx2` and the **OS trust store instead of certifi**. Our `_pools.py` builds `httpx.AsyncClient(http2=True)` and injects it into the Azure client, which is the wrong family under openai 3. Either pin `openai<3` in `pyproject.toml` for the first upgrade, or port the pool to httpx2 and add system CA certs to the image. | HARD (if unpinned) | `app/ai/voice/llm/_pools.py`, `Dockerfile` |
| 5 | `google-genai>=2.19.0` floor for the `google` extra **(new)**; our lock has 1.73.1 | lock refresh | `uv lock` |
| 6 | `anthropic<2` allowed **(new)**: we construct `AsyncAnthropicVertex` ourselves; anthropic 1 moves sampling params to `extra_body` inside pipecat; verify our `VertexAnthropicLLMService` against the SDK the lock picks | lock refresh | `llm/claude_vertex.py` |
| 7 | `daily-python>=0.29`, `deepgram-sdk<8`, `websockets` core, `nltk>=3.10`, `soundfile` and `python-dotenv` core, `mcp>=1.24` | lock refresh | `uv lock` |
| 8 | `pipecat.services.settings` no longer exports `NOT_GIVEN` (moved to `pipecat.utils.types`); `llm_context` still re-exports it, which is what `prompt_prefill.py` imports | OK | — |
| 9 | `language_to_elevenlabs_language`, `RTVIObserverParams`, `RTVIFunctionCallReportLevel`, `RTVIServerMessageFrame`, `FunctionCallResultProperties` all still importable from the paths we use | OK | — |

### 5.2 Flows behaviour changes (1.1 → 1.4 in-package, plus 1.9)

| # | Change | Class |
|---|---|---|
| 10 | Initial node follows its `context_strategy` (APPEND) instead of resetting; set `RESET` on the initial node for old behaviour | SILENT |
| 11 | `tts_say` / `end_conversation` append spoken text to context by default | SILENT |
| 12 | `FlowsFunctionSchema.handler` required; chat mode uses the schema as a container — verify every site passes a handler | HARD if violated |
| 13 | `role_messages` deprecated → `role_message` (applied via `LLMUpdateSettingsFrame(LLMSettings(system_instruction=…))`); `"system"` message at the start of `LLMContext` deprecated **(new)** | DEPRECATED (warns per manager) |
| 14 | 0/1-arg handlers, `FlowResult`, `@flows_direct_function`, `FlowManager(task=…)` deprecated | DEPRECATED |
| 15 | `NodeConfig` still `TypedDict(total=False)`; our extra keys keep working; `respond_immediately` honoured | OK |
| 16 | Empty `{}` result still skips the LLM re-run (aggregator gates on truthy `frame.result`); `NO_RESPONSE` is the explicit form | OK |

### 5.3 Pipeline / lifecycle defaults that change failure modes

| # | Change | Class | Our exposure |
|---|---|---|---|
| 17 | `TTSSpeakFrame.append_to_context` default `None→True` | SILENT | ~20 sites (hold messages, greetings, idle re-prompts, `WidgetVoiceBridge`, `tts_say`); early-speech already explicit. Audit each. |
| 18 | Function call exceeding `function_call_timeout_secs` (ours 10 s) is **cancelled** (`CancelledError` in the handler), then the LLM re-runs; timeouts no longer disarmed by intermediate updates (1.11) | SILENT | Flow handlers with DB writes / callbacks must be cancellation-safe. |
| 19 | `PipelineWorker(setup_timeout_secs=20, start_timeout_secs=20)`; a service that fails to connect in `setup()` is permanently unusable; services connect in `setup()` | SILENT | Slow provider connect at call start now tears the pipeline down. DragonTTS `start()` → `setup()`. Our Soniox override re-raises on connect failure. |
| 20 | `processor_unusable_policy` default `CONTINUE`; `max_consecutive_zero_audio_contexts=3`; `audio_out_write_timeout_secs=10`; websocket services stop reconnecting once unusable | SILENT | A dead TTS/transport leaves a silent call. Set `ProcessorUnusablePolicy.END`, subscribe to `on_usable_changed`, attach `ErrorObserver`. |
| 21 | **Websocket transport pacing (new, 1.10.0):** `BotStoppedSpeakingFrame` and `EndFrame` no longer arrive early on 8 kHz telephony | SILENT | Hangup timing on Twilio/Plivo/Exotel, our metrics turn commit, idle timer, `_speak_and_wait`. Re-test end-of-call flows. |
| 22 | STT streaming services no longer emit `ProcessingMetricsData`; websocket TTS neither; LLM TTFB redefined | SILENT | `MetricsCollectorProcessor` STT/TTS processing rows go empty; baselines shift (§6). |
| 23 | OTEL `gen_ai.provider.name` `az.ai.openai → azure.ai.openai`; `total_tokens` gross on Anthropic | SILENT | Langfuse filters / cost dashboards. |
| 24 | `ws_close_timeout=0.5`, `resampler_clear_after_secs=0.2`, `SingleClientWebsocketServerTransport` rename | SILENT | Re-test long-silence calls and warm-transfer teardown. |
| 25 | Turn-detecting STT + pinned non-external strategies: STT turn frames ignored; `ExternalUserTurnStrategies` interrupts by default | SILENT | Our Soniox `vad_force_turn_endpoint=False` + pinned strategies; decide explicitly (§3.4). |
| 26 | `idle_timeout_frames` counts transcription frames **(new)** | SILENT (good) | Idle cancel mid-utterance with VAD off goes away. |
| 27 | Deepgram `profanity_filter` no longer sent **(new)** | SILENT | Set `Settings(profanity_filter=True)` if the old behaviour is wanted. |
| 28 | STT only transcribes `InputAudioRawFrame` **(new)** | SILENT (good) | Our injected greeting audio no longer reaches Soniox. |
| 29 | `PipelineTask/Params/Runner`, `EndTaskFrame` family, `pipeline_task`, `tool_resources`, `StartFrame.audio_*`, `enable_async_tool_cancellation`, `AzureLLMService(api_version=)`, `pause_watchdog_timeout_s`, `LatencyBreakdown.chronological_events` | DEPRECATED | Warnings only; PEP 702 markers will light up pyrefly. |

### 5.4 Private seams we depend on (all present in 1.11.0; re-verify behaviour)

| # | Seam | Status in 1.11.0 | Action |
|---|---|---|---|
| 30 | `runner.utils._create_telephony_transport`, `parse_telephony_websocket` | present | transports connect in `setup()` |
| 31 | `SonioxSTTService._connect_websocket`, `_disconnect_websocket`, `_handle_transcription`, `_prepare_language_hints`, `_final_transcription_buffer`, `_last_tokens_received`, `FINALIZE_MESSAGE` | present; connect body now uses `self._websocket_connect()` and native endpoint settings; `_connect_websocket` swallows failures; `emit_stt_usage_metrics()` before each final; proposed-turn frames | shrink the subclass per §1 |
| 32 | LLM instance patching in `early_speech.py`: `get_chat_completions`, `process_frame`, `on_function_name_fragment` attribute; `tts.queue_frame` | present; `_process_context` still calls `get_chat_completions` at `base_llm.py:448` and parses `delta.tool_calls` | re-run the say-tool tests on 1.11; check the forced aggregation push |
| 33 | `prompt_prefill.py`: `service._client`, `service._settings`, `get_llm_invocation_params(system_instruction=, convert_developer_to_user=)`, `build_chat_completion_params`, `supports_developer_role` | present; `supports_developer_role` is a class attribute now | change the flows import root |
| 34 | `GeminiLiveLLMService._process_completed_function_calls`, `_END_FRAME_DEFERRAL_TIMEOUT_SECS`, `_inference_on_context_initialization` | present; upstream function-call path rewritten | re-derive or drop `BuddyGeminiLiveLLMService` override |
| 35 | `GeminiLLMAdapter._apply_thought_signatures_to_messages`, `_merge_parallel_tool_calls_for_thinking`; `service._adapter` swap | present; 1.6.0 grouped parallel tool calls for Gemini thinking, 1.11.0 sends placeholder `thought_signature` for foreign calls | likely deletable |
| 36 | `AnthropicLLMService._get_llm_invocation_params` (our prefill-on-assistant workaround) | present; **1.6.0 appends the `.` user turn upstream for Claude 4.6+** | likely deletable (double-append risk) |
| 37 | `GoogleLLMService._build_generation_params`, `_maybe_unset_thinking_budget`, `_tool_config`, `ThinkingConfig` alias; default model `gemini-3.6-flash`, `stream_idle_timeout_secs=20` | present | pin model explicitly |
| 38 | `AzureLLMService.create_client` / `_endpoint` / `_api_version` | present; `_use_v1_api` branch, `token_provider` | mirror the v1 branch in `_PooledAzureLLMService`; see #4 for the httpx family |
| 39 | `LLMUserAggregator._user_turn_controller.update_strategies`, `_params.user_mute_strategies`, `_user_is_muted` | present; controllers gained `start()/stop()`; `on_user_turn_inference_triggered` on the controller now carries a third `speculation` arg (we do not subscribe) | re-test node-level hot-swap |
| 40 | `DailyTransportClient.leave/cleanup/_leave/_cleanup`, `task._pipeline_start_event`, `transport._on_participant_joined` | present; teardown once, connect in `setup()` | re-test warm transfer + keepalive; zygote children |
| 41 | `TwilioFrameSerializer._hangup_attempted` via `transport.output()._params.serializer` | present | note #21 timing |
| 42 | `MCPClient._tool_wrapper` | present; `register_tools()` deprecated → `tools()`; failed calls now return the error text to the LLM | fine |
| 43 | RTVI protocol 2.x (`bot-output` spoken progress, `dtmf.buttons`, `bot-llm-marker`) | present | widget client SDK must accept 2.x |

### 5.5 Things that are fine

Deepgram `LiveOptions`/`live_options`; `OpenAISTTService(language, prompt, temperature)` (default model now `gpt-transcribe`, we pin `gpt-4o-transcribe` which shuts down 2027-02-26); `CartesiaTTSService`/`GenerationConfig`; `SarvamTTSService.Settings(pace, pitch, loudness, enable_preprocessing)`; `SmartTurnParams`, `LocalSmartTurnAnalyzerV3(cpu_count=…)`; all turn-strategy classes; `SoundfileMixer`; `DailyRunnerArguments`; `GoogleVertexLLMSettings`; `AnthropicLLMSettings.enable_prompt_caching`; `TTSService(push_start_frame, push_stop_frames)`, `run_tts(text, context_id)`; realtime event classes; all observers we import. `dragontts/` does not import pipecat.

---

## 6. Metrics and observability: what we have, what 1.11 offers, what to change

### 6.1 What we collect today

| Source | What | Where it goes | Gaps |
|---|---|---|---|
| `MetricsCollectorProcessor` (between TTS and transport) | per turn, per processor: `ttfb_ms[]`, `processing_ms[]`, `text_aggregation_ms[]`; function `latency_ms` from `FunctionCallInProgressFrame`→`ResultFrame`; turn commits on `BotStoppedSpeakingFrame` or `EndFrame` | `lead_calls.meta_data.pipecat_metrics` (JSONB) at `end_conversation`, merged across transfer generations | **Nothing in the repo reads it back.** No user-side latency (speech end → bot audio). No usage (tokens, cache, STT seconds, TTS chars) although `enable_usage_metrics=True`. Tool-call-only turns and speculative turns have no `BotStoppedSpeakingFrame`, so their metrics fold into the next turn. `processing_ms` for STT/TTS disappears on 1.8+. |
| `MetricsLogObserver` (prod), `LLMLogObserver` / `TranscriptionLogObserver` / `TurnTrackingObserver` (dev) | debug log lines | container logs | Debug level; not queried. |
| `UserBotLatencyObserver` | not attached (VAD off) | — | The one metric that matches what a caller feels is missing. |
| OTEL (`tracing_setup.py`, `PipelineTask(enable_tracing=True, conversation_id=…)`) | turn spans, `metrics.ttfb`, token usage, `gen_ai.*` | Langfuse; auto-eval scores incl. "HIGH LATENCY" | Pre-1.2.0 STT/TTS `metrics.ttfb` were wrong; LLM `output` missing on interrupted turns; TTS span text missing (regressed in 1.2, fixed 1.6). |
| RTVI `onMetrics` | `ttfb`, `processing` arrays | widget | 1.11 also sends `ttfa`, `ttfat`, `tokens`, `stt_usage`, `characters`. |
| Soniox subclass | `soniox_call_stats`, `soniox_watchdog_rescue`, `soniox_disconnect_flush`, `soniox_late_final`, per-final age | OpenObserve (log tokens) | Good pattern; keep. |
| `prompt_prefill` | `prefill: warmed … cached=…` | logs | Reads private `prompt_tokens_details`. |
| Chat mode | `[CHAT_METRICS]` line + `chat_turn_metrics` table (ttfui/ttlui, drops) | OpenObserve + DB + analytics | Voice has no equivalent structured line or table. |

### 6.2 What pipecat 1.11 emits that we do not use

| Metric | Class / event | Version | Meaning |
|---|---|---|---|
| Time to first audio | `TTFAMetricsData(ttfa, ttfb, leading_silence)` | 1.5.0 | how much "TTS latency" is silence padding (differs a lot between ElevenLabs, Azure, Sarvam) |
| Time to first answer token | `TTFATMetricsData(ttfat, ttfb, thinking_time)` | 1.8.0 | LLM TTFB plus reasoning/marker time; one per inference; a tool-call turn reports twice |
| Unified LLM TTFB | `TTFBMetricsData` | 1.8.0 | Anthropic/Google values rise; comparable across providers |
| Usage | `LLMUsageMetricsData(LLMTokenUsage: prompt, completion, total, cache_read_input_tokens, cache_creation_input_tokens (1.9.0), reasoning_tokens, audio tokens)`, `STTUsageMetricsData(audio_seconds)` (1.7.0), `TTSUsageMetricsData(characters)` | 1.7.0–1.9.0 | cost per turn, cache hit verification for the prefill |
| Smart-turn | `SmartTurnMetricsData(is_complete, probability, inference_time_ms)` | ≤1.8 | endpointing quality |
| **User → bot latency with contributions** | `UserBotLatencyObserver.on_latency_measured(seconds)`, `on_latency_breakdown(LatencyBreakdown)`: `contributions[]` (`key`, `label`, `owner`, `owner_kind ∈ service|setting|bot|pipeline`, `duration_secs`) that **sum to the interval**: endpointing wait `[config: VAD stop_secs]`, transcription `[SonioxSTTService#0]`, LLM inference, turn completion `[config: filter_incomplete_user_turns]`, speech synthesis, function handlers, pipeline hops; `user_turn_secs`; `measured_from ∈ user_silence|client_connected`; `on_first_bot_speech_latency` | 1.9.0 | the caller-felt number, attributed |
| Per-record service metrics | `ServiceMetricsObserver.on_service_latency(ServiceLatencyRecord: kind ∈ ttfb|ttfa|ttfat, processor, model, timestamp, seconds, ttfb_secs, leading_silence_secs, thinking_time_secs)`, `on_service_usage(ServiceUsageRecord)` | 1.9.0 | never summed; group by turn/model/deployment |
| Speech timeline | `SpeakingObserver.on_speech_event` (`user_speech_*`, `user_turn_*`, `bot_speech_*`, `interruption`, each with `started_at`) | 1.9.0 | turn boundaries as a policy we own, not `BotStoppedSpeakingFrame` |
| Function calls | `FunctionCallObserver.on_function_call_event` (`started`, `in_progress`, `completed|failed|timed_out|cancelled`; wait-to-run vs run time) | 1.9.0 | replaces our in-progress/result timing; sees timeouts and cancellations |
| Errors | `ErrorObserver.on_error(ErrorEvent: message, category, exception_type, processor, processor_usable)` | 1.9.0 | includes errors a `ServiceSwitcher` recovered from |
| Startup | `StartupTimingObserver` (`setup_phase_secs`, `start_phase_secs`, per-processor `setup_duration_secs`/`start_duration_secs`, `warmup.blocking_duration_secs`) | 1.8.0, 1.9.0 | proves what the zygote and pre-connect save |
| Markers | `LLMMarkerResponseFrame`, RTVI `bot-llm-marker` | 1.11.0 | marker accuracy on Hindi |

### 6.3 The one prerequisite: anchoring the user's silence

`UserBotLatencyObserver` measures from `VADUserStoppedSpeakingFrame` (`timestamp − stop_secs`). It never sets the anchor from `UserStoppedSpeakingFrame` or a `TranscriptionFrame`, so **with VAD off it reports only the first-bot-speech latency** and the per-turn breakdown never fires. Two options:

1. **Turn Silero VAD on** in the aggregator (`vad_analyzer=` is already plumbed; 1.8.0's `AudioVolumeTracker` makes `min_volume` safe on soft speech; VAD is also what makes `VADUserTurnStartStrategy` and smart-turn work). Recommended.
2. Keep VAD off and write a small app-side anchor: on the final `TranscriptionFrame` from Soniox, treat `time_now − max_endpoint_delay_ms` as the silence estimate and push a synthetic anchor into a subclass of the observer. Less accurate, more code.

### 6.4 Target per-turn record (voice)

One JSON object per user→bot cycle, keyed by `conversation_id`, `turn` (from `SpeakingObserver` `user_turn_stopped` count), `node`, `generation`:

| Field | Source |
|---|---|
| `user_turn_secs`, `endpointing_wait_ms`, `transcription_ms`, `llm_ms`, `turn_completion_ms`, `text_aggregation_ms`, `tts_ms`, `function_ms[]`, `pipeline_ms`, `total_ms`, `measured_from` | `on_latency_breakdown.contributions` (by `key`) |
| `stt.ttfb_ms`, `llm.ttfb_ms`, `llm.ttfat_ms`, `llm.thinking_ms`, `tts.ttfb_ms`, `tts.ttfa_ms`, `tts.leading_silence_ms` (each with `model`) | `on_service_latency` |
| `llm.prompt_tokens`, `completion_tokens`, `cache_read`, `cache_creation`, `reasoning_tokens`; `stt.audio_seconds`; `tts.characters` | `on_service_usage` |
| `service_tier_served` (Azure echo), `retry_fired` | app-side from the response / retry hook |
| `functions[]` with `wait_ms`, `run_ms`, `outcome` | `on_function_call_event` |
| `interrupted`, `bot_speech_ms`, `user_speech_ms` | `on_speech_event` |
| `errors[]` (`category`, `processor`, `usable`) | `on_error` |
| `say_tool_fired`, `say_tool_name`, `prefill_cached_tokens`, `soniox_watchdog_rescues` | app-side (early-speech router, prefill, Soniox stats) |
| call-level: `first_bot_speech_ms`, `startup.setup_ms`, `startup.start_ms`, `startup.warmup_blocking_ms`, `zygote_forked` | `on_first_bot_speech_latency`, `StartupTimingObserver`, app |

### 6.5 Concrete changes

1. **Replace `MetricsCollectorProcessor` with an observer-backed collector** (`processors/metrics_collector_processor.py` → `observability/voice_metrics.py`): attach `UserBotLatencyObserver`, `ServiceMetricsObserver`, `SpeakingObserver`, `FunctionCallObserver`, `ErrorObserver`, `StartupTimingObserver` via `get_observers()` in **production**, not only dev, and fold their events into the per-turn record above. Keep `MetricsFrame` de-dup by `frame.id`. Turn boundary = `user_turn_stopped` → next `user_turn_stopped`, so tool-only and speculative turns get their own row.
2. **Persist v2** to `lead_calls.meta_data.pipecat_metrics` with a `schema: 2` marker (existing rows stay readable), and **emit one `[VOICE_METRICS]` structured log line per turn** in the `[CHAT_METRICS]` style so OpenObserve can chart p50/p95 per template, provider, model, node and `service_tier_served` without a DB read. Add a `voice_turn_metrics` table only if the analytics UI needs joins (mirror of migration 032).
3. **Langfuse**: attach `total_ms`, `llm.ttfat_ms`, `tts.ttfa_ms`, `cache_read` as span attributes/scores on the turn span so the existing "HIGH LATENCY" auto-eval reads measured numbers instead of judging from the transcript.
4. **Re-baseline after upgrade**: STT `processing` panels go empty (1.8.0); LLM TTFB rises for Anthropic/Google (1.8.0); STT/TTS `metrics.ttfb` in OTEL were wrong before 1.2.0; `gen_ai.provider.name` renamed (1.6.0); `total_tokens` gross on Anthropic (1.7.0); `BotStoppedSpeakingFrame` timing shifts on telephony (1.10.0).
5. **RTVI**: extend the widget's `onMetrics` handler to `ttfa`, `ttfat`, `tokens`, `stt_usage`, `characters`, and enable `bot_llm_marker_enabled` when markers are trialled.
6. **Interim, on 1.1.0 (before the upgrade)**: `LLMUsageMetricsData` and `TTSUsageMetricsData` already exist on 1.1.0 — add them to the current collector now so cost-per-turn and the prefill's cache hit are visible; commit a turn on `UserStoppedSpeakingFrame` as well as `BotStoppedSpeakingFrame` so tool-only turns stop folding.
7. **Dashboards to build first**: (a) `total_ms` p50/p95 by template and node, split by `measured_from`; (b) stacked contributions per turn (endpointing / transcription / LLM / synthesis / pipeline) — this is the chart that tells us whether Azure or turn-end is the problem; (c) `llm.ttfat_ms` p95 by model and `service_tier_served`; (d) `cache_read / prompt_tokens` per turn index (turn 1 should be non-zero after the prefill); (e) `tts.leading_silence_ms` by provider; (f) function `wait_ms` vs `run_ms`; (g) Soniox rescues per 100 turns.

---

## 7. Suggested sequencing

1. **Mechanical upgrade branch** — pin `pipecat-ai[...]==1.11.0`, drop `pipecat-ai-flows`, pin `openai<3` for this step, `uv lock`; fix §5.1 #1–#3 and the flows import root in `prompt_prefill.py`; shrink the Soniox subclass; run the import-smoke, `pyrefly`, `pytest`.
2. **Behaviour audit** — #17 (every `TTSSpeakFrame`), #18 (handler cancellation), #19/#20 (timeouts + unusable policy + `ErrorObserver`), #21 (telephony hangup timing), #25 (turn-strategy decision), #34–#36 (re-derive or delete overrides), dashboards (#22–#23). Ship with `processor_unusable_policy=END` and the §6.5 observers attached in prod.
3. **Metrics v2** (§6.5) — land before any latency work so every change is measured against the contributions chart. Includes turning VAD on.
4. **Latency pass** — Soniox external strategies or smart-turn v3.2, `retry_on_timeout`, HTTP/2 pool in the voice subprocess (httpx family per openai version), TOKEN mode A/B for flash, Azure Global-Standard deployment on a priority-eligible model with `service_tier="priority"` and a smoothed dispatch ramp, `NO_RESPONSE` for say tools.
5. **STT/TTS pilots** — Deepgram Flux Multilingual with eager end-of-turn on a Hindi cohort (the only speculative path that speaks Hindi); ElevenLabs v3 Conversational via `ElevenLabsDialogueTTSService` behind DragonTTS on expressive nodes; Sarvam bulbul v3 and saaras:v4 realtime; `stt-benchmark` on our recordings for TTFS P99 and WER.
6. Retire `docs/pipecat-upgrade-recommendations.md` once step 1 lands; fix the test path cited in `docs/TOOL_BASED_SPEECH_ARCHITECTURE.md`.

---

## Sources

- pipecat CHANGELOG (`v1.2.0`…`v1.11.0`) and source tree at `v1.11.0`; pipecat-flows CHANGELOG 1.1.0–1.4.0
- ElevenLabs: Models overview; Text-to-Dialogue realtime websocket guide; Eleven v3 blog
- pipecat docs: ElevenLabs TTS reference; STT latency tuning; `stt-benchmark`; smart-turn README (v3.2, 23 languages incl. Hindi/Marathi)
- Microsoft Learn: Enable priority processing for Microsoft Foundry Models (2026-07-13)
- Deepgram: Flux Multilingual launch (2026-04-29; 10 languages incl. Hindi, code-switching); Cartesia: Ink-2 (English-only)
- pipecat-ai/nemotron-january-2026 streaming pipeline architecture; FutureAGI "12 techniques" (2026)
- Sarvam bulbul v3 blind-study claims and Cartesia sonic-3 latency figures are vendor/third-party numbers; verify on our own audio.
