# TODO

Living index of follow-ups for this repo. Append new items at the bottom of the relevant section. Remove rows when the underlying work lands. Last updated: 2026-05-02 (post pipecat-ai 1.1.0 + pipecat-ai-flows 1.0.0 migration).

---

## 1. Pipecat 1.0 — subtle risks worth knowing

These are not bugs in our code; they are semantic shifts introduced by the bump. Production keeps working, but ops/monitoring should be aware.

| # | Risk | Where it lives | Recommended action |
|---|---|---|---|
| 1 | **WebSocket reconnect failures are now non-fatal.** Before 1.0, exhausted retries on Soniox / Deepgram / ElevenLabs / Cartesia killed the pipeline (clean call-end). 1.0 emits an `ErrorFrame` instead and the pipeline keeps running with a dead service → silent dead-air. | Anywhere in the pipeline that uses a websocket-backed STT/TTS service. | Add an alert on `ErrorFrame` from STT/TTS providers (or wire `pipecat.processors.service_switcher.ServiceSwitcher` upstream of STT/TTS for primary→fallback failover). |
| 2 | **`group_parallel_tools=True` is the new pipecat default.** Multi-tool LLM responses now produce exactly one inference after the last call completes (was: one inference per result). | All flows that issue parallel function calls. | Watch the first multi-call template; if you need per-result LLM commentary back, set `group_parallel_tools=False` on the LLM service. |
| 3 | **Pipecat-flows raises `FlowError` / `InvalidFunctionError` instead of `ValueError`.** Previously bare `except ValueError` could swallow flow validation errors. | `app/ai/voice/agents/breeze_buddy/template/*.py`, anything that constructs `FlowsFunctionSchema` or calls `set_node_from_config`. | Don't `except ValueError` around flow-construction code; catch the specific flow exceptions instead. |
| 4 | **`mem0/memory.py` is unimportable under 1.0.** Still references removed `OpenAILLMContext` / `OpenAILLMContextFrame` / `LLMMessagesFrame`. The file is dead code (only call site is commented out at `automatic/__init__.py:57-58`), so prod is safe — but anyone reviving mem0 must port it first. | `app/ai/voice/agents/automatic/services/mem0/memory.py` | Either delete the file outright, or port it to `LLMContext` + `LLMMessagesUpdateFrame(run_llm=True)` if mem0 comes back. |
| 5 | **`LLMSpyProcessor.stt_mute_filter` constructor param is a dead branch.** Caller in `automatic/__init__.py:497` always passes `None`, but the conditional `_stt_mute_filter.process_frame(...)` block is still in `processors/llm_spy.py:211-213`. Cosmetic clutter; not load-bearing. | `app/ai/voice/agents/automatic/processors/llm_spy.py:146,153,211,213` | Drop the parameter and the dead branch in a small follow-up PR. |
| 6 | **DB-stored templates use `role: "system"` for `task_messages`.** Pipecat-flows 1.0 prefers `role: "developer"` for application instructions. Pipecat still accepts `"system"` (advisory, not a hard break), but the new role is semantically correct and helps providers that distinguish developer-tier instructions. | DB tables holding template JSON; `app/ai/voice/agents/breeze_buddy/examples/templates/*.json`. | Optional one-shot SQL: `task_messages` rows with `role: "system"` → `role: "developer"`. Verify against template authoring tools first. |

---

## 2. Open in-code TODOs

Direct grep of `# TODO` markers in `app/`. Sourced 2026-04-27.

| File:line | Note |
|---|---|
| `app/ai/voice/agents/breeze_buddy/managers/calls.py:584` | Advance `next_attempt_at` by the rate-limit window so the scheduler does not immediately re-pick a rate-limited lead and re-fire the alert on the next cycle. |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py:235` | Add a breeze-buddy-specific context summarizer. Pipecat 1.0 has `LLMContextSummarizer` building blocks but no out-of-the-box summarizer wired into telephony pipelines yet. Long conversations risk hitting model context limits. |
| `app/api/routers/breeze_buddy/telephony/answer/handlers.py:390` | Twilio block-response branch not yet implemented (currently returns a generic JSON fallback). Plivo/Exotel paths exist; Twilio needs the equivalent TwiML response. |
| `app/api/routers/breeze_buddy/telephony/answer/handlers.py:678` | Twilio block-handling for the answer flow not yet implemented (matches the gap above). |
| `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` (`get_observers`) | Per-turn user→bot latency observability is currently OFF. Pipecat's `UserBotLatencyObserver` is VAD-coupled (`on_latency_measured` only fires on `VADUserStoppedSpeakingFrame`), and prod runs with `BREEZE_BUDDY_ENABLE_VAD=false`, so attaching it would emit only one greeting-latency event per call. Either enable VAD in prod, or write a VAD-free observer that measures `UserStoppedSpeakingFrame → BotStartedSpeakingFrame` and pushes the value onto the active OTEL span (so Langfuse sees it). |

---

## 3. Pipecat 1.0 features adopted on 2026-04-27

Recorded here so reviewers have a single place to see what behavioural shifts we deliberately opted into.

- **Async function calls for HTTP global functions.** `GlobalHttpFunction.cancel_on_interruption` defaults to `False` (`app/ai/voice/agents/breeze_buddy/template/types.py:1037-1041`) — the LLM keeps the user engaged while a slow HTTP call runs; the result is injected back as a developer message that re-triggers inference. Builtins (`GlobalBuiltinFunction.cancel_on_interruption=True`, `types.py:1075-1079`) stay synchronous because warm-transfer / end-conversation are control-flow critical.
- **Anthropic Vertex prompt caching.** `claude_vertex.py:127-136` sets `enable_prompt_caching=True` on `AnthropicLLMSettings`. Long system prompts + per-template instructions get cached across turns — cuts Vertex Anthropic spend and time-to-first-token. (Note: replaces the removed `enable_prompt_caching_beta` flag.)

---

## 4. Pipecat 1.0 features available but not yet adopted

Lower-priority wins. Pull from here when next touching the relevant area.

- **Streaming intermediate function-call results** (`result_callback(..., is_final=False)`). Builds on the async function feature — useful for "I'm still checking..." UX during long HTTP calls. Plug-in point: `app/ai/voice/agents/breeze_buddy/handlers/transport/http_handler.py`.
- **`LLMMessagesTransformFrame`.** Replaces the racy "snapshot context, mutate, push update" pattern. Affects warm-transfer prep and end-conversation hooks. Refactor pass, not a P0.
- **`ServiceSwitcher` for STT/TTS failover.** Pairs with risk #1 above. Wraps a primary + fallback websocket service so a dead provider auto-fails over instead of silently dying.
- **ElevenLabs `pcm_32000` / `pcm_48000` sample rates.** Telephony stays on mu-law 8 kHz, so this only matters if Daily.co web ever needs higher fidelity. Low priority.
- **`LLMContext.get_messages(truncate_large_values=True)`.** Useful for emitting context snapshots into Langfuse/logs without binary blobs. Cosmetic.

---

## 5. Provider-specific follow-ups

Discovered during the 1.1.0 review pass. None of these are blockers; tracking so we don't lose context.

### Sarvam TTS — migrate to bulbul:v3, then delete the auto-detection wrapper

**Step 1 — switch default model to `bulbul:v3`.** Pipecat 1.1.0 already supports it as a first-class model option. Update `BB_VOICE_PROVIDER_DEFAULTS("sarvam").model` (Redis) and the per-template defaults. Caveats:
- V3 ignores `pitch` and `loudness` (pipecat warns and clamps; harmless).
- V3 `pace` range is 0.5–2.0 (V2 was 0.3–3.0). Existing pace values outside this clamp.
- Default sample rate is **24000 Hz** on V3 vs 16000 Hz on V2. Verify the telephony pipeline correctly resamples to 8kHz mu-law for outbound calls before flipping prod.
- V3 is in unlimited free preview until 2026-02-28 — good window for a soak.

**Step 2 — audit prod templates** for any conversation flows that produce cross-Indian-script LLM output (e.g., a single call where the LLM emits Telugu *and* Hindi). Hindi+English (Hinglish) does NOT count — V3 handles that natively in one request, so the wrapper would be redundant for those flows.

**Step 3 — delete `LanguageAwareSarvamTTS` + `detect_script` + `SCRIPT_RANGES` + `SCRIPT_TO_SARVAM_LANG`** from `app/ai/voice/tts/sarvam.py` (~115 lines, one file). Only safe if step 2 confirms zero cross-Indian-script flows. If step 2 finds even one, leave the wrapper — V3's text-normalization is still driven by the per-request `target_language_code` and would mangle off-script text.

Also worth fixing as a one-liner whether or not we delete the wrapper: `_switch_language_if_needed` assigns a bare `str` into `self._settings.language` after the first script switch, while `build_sarvam_tts` initializes it as a `Language` enum. Cosmetic type drift only — `Language` is a `StrEnum` so equality and JSON serialization both work — but if pipecat ever tightens the field type to `Language` only, this breaks. Fix at `app/ai/voice/tts/sarvam.py:161`: wrap the lookup in `Language(...)`.

### Soniox STT — `SonioxSTTServiceWithEndpointDelay` is fragile

Our subclass at `app/ai/voice/stt/soniox/service.py` exists solely to inject `max_endpoint_delay_ms` into the WebSocket connection config. Pipecat 1.1.0 does not expose this field on `SonioxSTTSettings` and `_connect_websocket` does not forward `s.extra` either, so a subclass override is currently the only option. The override copies the entire body of pipecat's private `_connect_websocket` — any restructure upstream silently drops our injection.

**Action**: file an upstream PR adding `if isinstance(s.extra, dict): config.update(s.extra)` to `pipecat/services/soniox/stt.py:_connect_websocket` (mirrors what `DeepgramSTTSettings` already does via `_sync_extra_to_fields`). Once merged, our subclass goes away — we just pass `extra={"max_endpoint_delay_ms": ...}` to `SonioxSTTService.Settings`. Tiny PR, generally useful, lets us delete `app/ai/voice/stt/soniox/service.py` entirely.
