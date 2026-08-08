# Scripted Responses → Speculative Interim-Driven Fast Turns (v2)

- **Status:** Phase-0 spike **DONE — approach validated.** Ready for implementation.
- **Created:** 2026-08-06 · **v2 (speculative):** 2026-08-07
- **Branch:** `improve-realtime-gemini` (target runtime: **pipecat pipeline** — Soniox → Azure GPT → DragonTTS)
- **Goal:** Make voice turns feel instant by hiding LLM latency *behind the user's own speech* via speculative classification of interim transcripts, committing a pre-computed answer on turn-final.

> v2 **supersedes** v1's "LLM responds on final only" mechanism. v1's verified facts
> (DragonTTS splitting, `tool_choice`, helpers) are reused — see §5.

---

## 1. The idea (one paragraph)

Soniox streams **partial** transcripts as the user speaks (`हाँ` → `हाँ जी` →
`हाँ जी कन्फर्म` → final). We send **each interim** to a classifier LLM that may
reply with only: **a phrase number (1–N)**, **`.`** (stay silent — insufficient
info), or a **function call**. Speculative answers are *held* (never played
pre-final) and **self-correct**: same number repeated → suppress; number changed
→ swap the held intent. On **turn-final** we commit the latest held result and
play it. Because the answer was computed *while the user was still speaking*, the
LLM's TTFT is hidden behind speech — perceived latency ≈ endpointing + TTS-play.

```
user speech ─► Soniox interims ─► [classifier LLM, ~1 token] ─► held intent
                                                                 (dedup 5s / swap on change)
user stops ─► final transcript ─► COMMIT latest held intent ─► DragonTTS (cache HIT) ─► audio
                                      (never speaks over the user)
```

### Why it's fast (the part that matters)
The win lands **only if the answer is ready by final** → on final we commit the
**latest speculative result**, we do *not* fire a fresh call (that would re-incur
TTFT). If the last interim's call is still in-flight at final, **await it briefly**
rather than fire new.

```
TODAY (scripted, final-only):   stop → endpoint ≤500ms → fire LLM → TTFT ~315ms → TTS ~100ms  ≈ 915ms
THIS (speculate, commit-final): stop → endpoint ≤500ms → [LLM done during speech] → TTS ~100ms ≈ 600ms
```
Caveat: the win scales with turn length (one-word "हाँ" has ~1 interim → little to hide).

---

## 2. Phase-0 spike results (2026-08-07) — `scripts/speculative_classify_spike.py`

Hand-labeled Hindi COD turns (partial→final); forced `json_schema` enum; 1 call/interim.

| model | final acc | `.` discipline | wrong-commit | invalid | avg TTFT | tokens |
|---|---:|---:|---:|---:|---:|---:|
| **azure gpt-4o-automatic** | 88% | 70% | 3 | 0 | **275ms** | 6.0 |
| **azure gpt-5.4-mini-2** (`reasoning_effort=minimal`) | **100%** | **90%** | 1 | 0 | 456ms | 13.0 |
| **gemini 3.5-flash-lite** (no thinking) | 100% | 80% | 2 | 0 | 772ms | 2.4 |

**Findings:**
1. **Approach works.** 0 invalid choices across 24×3 calls (the forced enum guarantees valid output). 88–100% final-turn accuracy. The #1 risk is retired.
2. **`.` discipline is the lever** — staying silent on ambiguous early partials (70–90%). Crucially, **every** wrong early commit *self-corrected* by final (e.g. gpt-4o: `मैं अभी`→`4` too early → converges to `4`; gemini: `हाँ`→`10`→`.`→`1`). Because we commit-on-final, these never play — they're pure (harmless) speculation.
3. **Model pick:** **gpt-5.4-mini-2** — 100% final + 90% discipline at 456ms (hides cleanly behind multi-word turns). gpt-4o is fastest (275ms) but trigger-happy + 88% final; gemini is too slow (772ms ≥ endpointing, little left to hide). *Trade-off:* gpt-4o for raw speed if a final re-check covers its accuracy gap.

---

## 3. Implementation components (mapped to files)

| # | Component | File(s) |
|---|---|---|
| 1 | Global phrase table `ScriptedResponse{id,text,transition_to?}` on `ConfigurationModel` (1–40, per user decision) | `template/types.py` + `field_reference.json` |
| 2 | `SpeculativeScriptedClassifier` — standalone async Azure client, forced `json_schema` enum, ~1 token, returns `{choice, ...}` | new `app/ai/voice/agents/breeze_buddy/llm/speculative_classifier.py` |
| 3 | `SpeculativeScriptedProcessor` (new pipecat processor) — on `InterimTranscriptionFrame` fire classifier (every interim, per decision); hold intent; dedup-same-number within 5s; swap on number-change; cancel superseded in-flight | new under `agents/breeze_buddy/processors/` |
| 4 | **Final-gate commit** — on `TranscriptionFrame`/turn-stop: await latest in-flight if needed, then commit → DragonTTS phrase / function-on-final / silence; **suppress the normal LLM turn** for scripted nodes | the processor + pipeline wiring in `agent/pipeline.py` |
| 5 | System-prompt rules (number/`.`/function, speak-then-call, prefer `.` on ambiguity) | classifier service |
| 6 | Observability: chosen `id`, DragonTTS hit/miss, speculative-vs-committed, fallback rate | processor + metrics |

### Key integration decision (§6 open Q1)
For scripted turns the **main pipecat LLM user-turn must be bypassed** — the
`SpeculativeScriptedProcessor` owns the turn (classify → phrase → TTS). Keeping
both would double-speak. Recommendation: scripted nodes opt in via template config;
when opted in, the processor intercepts interims+final and the normal
`LLMUserAggregator`→LLM path is not driven for those nodes. Non-scripted nodes are
unchanged.

## 4. Phases
1. **Classifier service + phrase-table model** — standalone, unit-testable (extend the spike).
2. **Processor on one COD node** — interim→classify→hold + commit-on-final + DragonTTS. Measure perceived latency (transcript→first-audio) vs baseline.
3. **Self-correction** (dedup/swap), **function-on-final + speak-then-call**.
4. **Hardening** — silence fallback, in-flight cancel/concurrency, barge-in interplay, eval harness on real transcripts.

## 5. Reusable facts (verified, from v1)
- **DragonTTS already splits on `।`/`॥`** — pipecat `match_endofsentence` (`utils/string.py:60-61`). Push one `TTSSpeakFrame`; cache keys are per-sentence. Author static/variable as separate sentences for independent cache hits.
- **`pipecat_flows` never sets `tool_choice`** → defaults `auto`. Strict mode = `context.set_tool_choice("required")` (not needed for v2 — the forced `json_schema` enum already constrains output).
- **Helpers exist:** `context.queue_tts_filler(text)` (context.py:118), `replace_placeholders(text, vars)` (field_resolver.py:42), `context.bot.template_vars`.

## 6. Open questions
1. **Confirm scripted nodes bypass the main LLM turn** (recommend yes) — §3 decision.
2. **Phrase-table placement** — `ConfigurationModel.scripted_responses` (recommend) vs a top-level template field.
3. **Cost** — every-interim firing; monitor call-count/turn on real calls (gpt-5-mini is cheap; ~2–4 calls/turn typical).

---

## Sources
- [Forcing function calling via tool_choice: "required" — OpenAI](https://community.openai.com/t/new-api-feature-forcing-function-calling-via-tool-choice-required/731488)
- [Structured outputs / enum enforcement — OpenAI](https://community.openai.com/t/structured-outputs-enforce-enum-specified-values/1124602)
- Spike harness: `scripts/speculative_classify_spike.py`
