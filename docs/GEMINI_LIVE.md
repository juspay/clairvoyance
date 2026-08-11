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
| Builder (`GeminiRealtimeConfig` → `GeminiLiveLLMService`) | `app/ai/voice/llm/realtime/gemini_realtime.py` |
| Provider factory (resolves api key, forwards params) | `app/ai/voice/llm/realtime/factory.py` |
| `RealtimeConfig` fields (provider/model/voice/language/thinking/silence) | `app/ai/voice/llm/types.py` |
| Pipeline wiring (realtime branch) | `app/ai/voice/agents/breeze_buddy/agent/pipeline.py` (`is_realtime`) |
| Agent glue (greeting and idle handling) | `app/ai/voice/agents/breeze_buddy/agent/__init__.py` |

Surface: **Gemini Developer API only** (no Vertex in production). Auth via the
Gemini API key resolved in the factory.

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
    "silence_duration_ms": 600                  // optional; server-side VAD end-of-speech
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
  false interruptions (the opposite of "fewer unnecessary turns"). Unset → Gemini's
  server-side default.

All four optional fields are forwarded only when set; unset → pipecat/Gemini
defaults apply (`factory.py` → `gemini_realtime.py`).

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

## Greeting behaviour & `dial_tone`

The opening greeting can be played two ways:

1. **Out-of-band** (default when a greeting is cached): BB synthesizes the
   `initial_greeting` and pushes it directly to the transport (telephony `playAudio`
   / Daily `OutputAudioRawFrame`). Fast — no wait for Gemini's first token.
2. **Gemini speaks first**: when no greeting is cached and the realtime LLM should
   open, Gemini generates the first utterance.

When there is **no cached greeting**, BB historically falls back to a `dial-tone.wav`
(`utils/common.py`). For realtime that collides with Gemini's own opening response,
so set:

```jsonc
"dial_tone": false   // under configurations; default true (legacy)
```

Effect: no greeting cached + `dial_tone:false` → **nothing** plays out-of-band and
the realtime LLM speaks first. A cached greeting always plays regardless of this
flag. (Default `true` preserves legacy behaviour for non-realtime templates.)

> ⚠️ **Known issue (pre-fix):** returning `None` from the greeting-prep skip path is
> currently treated by `send_initial_greeting` as a *failure* and recorded into
> `lead.metaData.errors`. So a realtime template with no cached greeting logs a
> spurious "Failed to prepare greeting payload" error on every call. Fix pending —
> the skip must be distinguished from a real failure.

## Idle handling

Realtime **skips** the agent-owned post-greeting idle timer
(`_is_realtime_llm()` guards both telephony and Daily call sites): that wall-clock
timer raced Gemini's latency-delayed turn-start signal and re-fired "are you
there?" mid-reply, dropping in-flight audio (the original "not listening" bug).

Realtime instead relies on the aggregator's `on_user_turn_idle`
(`pipeline.py`, `UserIdleController`).

> ⚠️ **Known gap:** the idle controller arms only on `BotStoppedSpeakingFrame`. The
> out-of-band telephony greeting emits no such frame, and the initial node does not
> respond immediately after a pre-played greeting — so a caller who picks up and
> **stays silent after the greeting** may receive no nudge and no BUSY outcome until
> the provider's own timeout. Verify with a silent-caller test.

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

- Spurious greeting-prep error on the no-greeting realtime path (see `dial_tone`).
- Silent-after-greeting idle gap (see Idle handling).
- No Pydantic validation on `thinking_level` / bounds on `silence_duration_ms`.
- Undocumented `language` effect on the 3.1 live model.
- `_is_realtime_llm()` duplicates realtime-detection logic in `pipeline.py` — DRY.
