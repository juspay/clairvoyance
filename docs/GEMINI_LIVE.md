# Gemini Live — production realtime integration

Gemini Live is Breeze Buddy's speech-to-speech **realtime LLM** provider. It runs
STT + LLM + TTS + turn-detection **server-side** over one persistent WebSocket, so
the BB pipeline wires a single `GeminiLiveLLMService` in place of the usual
separate STT → LLM → TTS chain. This is the lowest-latency path for telephony and
widget voice.

> For the **standalone test harness** (no Pipecat pipeline, mic/speaker against the
> raw API), see [`GEMINI_LIVE_TEST.md`](./GEMINI_LIVE_TEST.md).

## Where it lives

| Concern | File |
| --- | --- |
| Builder (`GeminiRealtimeConfig` → `BuddyGeminiLiveLLMService`) | `app/ai/voice/llm/realtime/gemini/realtime.py` |
| Opening-line generator (per-template pre-generated greeting) | `app/ai/voice/llm/realtime/gemini/opening_line.py` |
| Provider factory (resolves api key, forwards params) | `app/ai/voice/llm/realtime/factory.py` |
| `RealtimeConfig` fields (provider/model/voice/language/thinking/silence) | `app/ai/voice/llm/types.py` |
| Pipeline wiring (realtime branch) | `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` (`is_realtime`) |
| Agent glue (greeting and idle handling) | `app/ai/voice/agents/breeze_buddy/agent/__init__.py` |
| Opening-line cache (store/check/invalidate) | `app/ai/voice/agents/breeze_buddy/managers/utils.py` (`ensure_realtime_opening_line_cached`) |
| Save-time validation + regeneration | `app/api/routers/breeze_buddy/templates/handlers.py` |

Surface: **Gemini Developer API only** (no Vertex in production). Auth via the
Gemini API key resolved in the factory.

> The builder produces **`BuddyGeminiLiveLLMService`**, a thin subclass of
> pipecat's `GeminiLiveLLMService`. pipecat 1.1.0 delivers async-tool results as a
> `developer` message that the Gemini adapter maps to plain user text — invisible
> to the function-response loop, so an awaited async function (e.g. a global
> function's HTTP result) stalls the model until the customer speaks. The subclass
> re-sends finished results via `send_tool_response` plus the Gemini-3.x realtime
> nudge (`send_realtime_input(text=" ")`), mirroring pipecat's own sync-result
> path. Re-evaluate on the next pipecat upgrade.

## Model & voice defaults

- Default model: `gemini-3.1-flash-live-preview` (Developer API; server-side VAD).
  Templates override via `realtime.model`.
- Default voice: `Kore`. Override via `realtime.voice`.

## Configuration

Set on the template's `configurations.llm_configurations.realtime`:

```jsonc
"llm_configurations": {
  "realtime": {
    "provider": "gemini",
    "model": "gemini-3.1-flash-live-preview",   // optional; default above
    "voice": "Kore",                            // optional; default Kore
    "language": "hi",                           // optional; BCP-47, see note
    "thinking_level": "minimal",                // optional; minimal|low|medium|high
    "silence_duration_ms": 600,                 // optional; server-side VAD end-of-speech (>=1)
    "endframe_deferral_timeout_secs": 1         // optional; default 1
  }
}
```

### Realtime field reference

- **`model`** — Gemini Live model id. Defaults to `gemini-3.1-flash-live-preview`.
- **`voice`** — Gemini prebuilt voice name. Defaults to `Kore`.
- **`language`** — BCP-47 code (`hi`, `ta`, `hi-IN`, …). Sent on the wire only when
  set. *Caveat:* on the 3.1 live model the effect is undocumented; Hindi output
  normally comes from the system prompt, not this field. Confirm with a live test.
- **`thinking_level`** — Gemini 3.x reasoning depth: `minimal` / `low` / `medium` /
  `high`. 3.1-flash-live defaults to `minimal` for lowest latency. Ignored by 2.5
  native-audio. **Not validated** today — a typo passes through and fails at the
  Gemini API on first turn, so set it carefully.
- **`silence_duration_ms`** — server-side VAD end-of-speech threshold (ms): how long
  a pause ends the user's turn. **The dominant per-turn latency lever.** Recommend
  **500–800 ms**. Do **not** go below ~400 — natural Hindi/Hinglish pauses are
  150–400 ms, so 100–200 ms will cut the caller off mid-sentence and *increase*
  false interruptions (the opposite of "fewer unnecessary turns"). Validated `>= 1`;
  unset → Gemini's server-side default.
- **`endframe_deferral_timeout_secs`** — cap on the deferred `EndFrame` queued by
  `finish_call`/`end_conversation`. pipecat otherwise parks it for up to 30 s when
  the bot considers itself mid-turn (a function-call-only turn never emits
  `turn_complete`), leaving the line open until the customer hangs up. Default `1`
  second; `0` releases immediately.

All optional fields are Gemini-only — every other realtime provider ignores them —
and are forwarded only when set; unset → pipecat/Gemini defaults apply
(`factory.py` → `realtime/gemini/realtime.py`).

## Pipeline topology

```
transport.input()  →  LLMUserAggregator  →  GeminiLiveLLMService  →  transport.output()  →  assistant_aggregator
```

No separate STT or TTS service is created (`pipeline.py` realtime branch returns
`(None, realtime_llm, None)`). Gemini emits transcription + turn-start/stop frames
from its server-side STT/VAD; the user aggregator's default strategies react to
those (it is given no custom strategies).

## Noise cancellation (AIC)

Already wired — **no code change needed**. The transport input filter
(`agent/transport.py`, `get_transport_params`) attaches an `AICFilter` to **every**
transport (8 kHz model for telephony, 16 kHz for Daily), and the realtime pipeline
uses that same `transport.input()`. Enable per template:

```jsonc
"noise_filter": { "enable": true, "provider": "aic", "model": "noise_cancellation" }
// or "voice_focus" to isolate the foreground speaker
```

Prerequisites: `BREEZE_BUDDY_AIC_LICENSE_KEY` set and the model file present
(`transport.py` logs a warning and proceeds without filtering if either is
missing). AIC adds a small processing cost — weigh against the min-latency goal.

## Opening line (per-template, static-only) & `dial_tone`

The opening line is **pre-generated once per template** — not per lead — with the
template's exact Live model/voice/language, so the pre-played audio is
indistinguishable from the live session's own speech.

**Static-only.** `initial_greeting` for a Gemini-realtime template must be plain
text — `PUT/POST /templates` **rejects `{placeholder}` greetings with a 422** (there
is no lead payload at generation time; personalise in the conversation instead).
Non-Gemini templates keep full variable support on the TTS path. A template
stored before this enforcement that still has variables simply skips pre-play
(warn log, LLM speaks first) until re-saved. Pre-generation is automatic —
Gemini realtime + a non-empty `initial_greeting` is the whole trigger; clearing
the greeting disables it.

**Lifecycle.**

- **Template save (create/replace):** a background task regenerates the line with
  `force=True` — so voice/model/greeting edits never serve stale audio. Failure
  only warns; the first call regenerates lazily.
- **Invalidation:** every PUT/DELETE drops the cached key alongside the template
  cache (`template/cache.py`), so a removed/disabled greeting can't outlive its
  edit — invalidation is the correctness mechanism (no TTL).
- **Call time (dispatch worker, pre-dial):** a single Redis `GET` on the happy
  path. On a miss (failed save-time generation) the worker awaits generation
  before dialing — bounded at 30 s (`DEFAULT_GENERATION_TIMEOUT_SECONDS`) with a
  35 s outer backstop, suspending only that worker's slot, then dials fail-open
  (LLM speaks first) on timeout.
- **Playback:** at connect the cached audio plays out-of-band immediately
  (telephony `playAudio` / Daily `OutputAudioRawFrame`), and the template's
  `initial_greeting` seeds the LLM context so Gemini never repeats the line.
  The entry lives in the **shared persistent static key**
  (`greeting:template:{id}`, raw base64 mulaw — the same key and format the
  TTS path uses; the text is derived from the template config at read time)
  and is **read without delete** — one generation serves every call until the
  next template edit.

When there is **no cached greeting** (feature disabled, greeting-less, or a
variable greeting on a legacy template), BB historically falls back to a
`dial-tone.wav` (`utils/common.py`). For realtime that collides with Gemini's own
opening response, so set:

```jsonc
"dial_tone": false   // under configurations; default true (legacy)
```

Effect: no greeting cached + `dial_tone:false` → **nothing** plays out-of-band and
the realtime LLM speaks first. A cached greeting always plays regardless of this
flag. (Default `true` preserves legacy behaviour for non-realtime templates.)

## Idle handling

Realtime **skips** the agent-owned post-greeting idle timer
(`_is_realtime_llm()` guards both telephony and Daily call sites): that wall-clock
timer raced Gemini's latency-delayed turn-start signal and re-fired "are you
there?" mid-reply, dropping in-flight audio (the original "not listening" bug).

Realtime instead relies on the aggregator's `on_user_turn_idle`
(`pipeline.py`, `UserIdleController`).

> ⚠️ **Known gap:** the idle controller arms only on `BotStoppedSpeakingFrame`. The
> out-of-band telephony greeting emits no such frame — but the input-gate half of
> this gap is fixed: realtime flows always push the initial run frame
> (`respond_immediately=True`) with initial inference suppressed, so Gemini's mic
> input gate opens and customer speech after the greeting **is** honoured. What
> remains: a caller who picks up and **stays silent** gets no *proactive* nudge and
> no BUSY outcome until the provider's own timeout. Verify with a silent-caller
> test.

## Latency notes

- **`silence_duration_ms`** is the dominant per-turn lever (see above).
- The **system prompt + tools are sent once**, at connect/reconnect — not per turn.
  The ~33 KB prompt is amortized by server-side KV cache; it is not a per-turn cost.
- The greeting plays **out-of-band for speed**, which means **no barge-in during the
  greeting** — Gemini can't interrupt a `playAudio`. Speech during the greeting
  isn't honoured until it ends. (Routing the greeting through Gemini's output would
  enable barge-in, at the cost of greeting latency.)
- Verified end-to-end: post-greeting user turn → bot reply at ~0.03 s TTFB (the wait
  is the VAD `silence_duration_ms`, not the pipeline).

## Open items

- Silent-after-greeting callers get no proactive nudge (see Idle handling — the
  input-gate half is fixed; the proactive-nudge half is not).
- No Pydantic validation on `thinking_level` (`silence_duration_ms` is validated
  `>= 1`).
- Undocumented `language` effect on the 3.1 live model.
- `_is_realtime_llm()` duplicates realtime-detection logic in `pipeline.py` — DRY.
