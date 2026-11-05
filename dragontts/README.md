# dragonTTS

TTS caching proxy (FastAPI): caches text-to-speech audio to cut latency and cost
on repeated synthesis. Multi-provider (Cartesia / ElevenLabs / Sarvam / Gemini),
SQLite + filesystem store, write-through caching with length-scaled TTL.

## Run locally

The folder is self-contained — a venv, deps, and the `app` package all live
inside it. Always `cd` here and run from this folder.

### 1. Configure env
```bash
cp .env.example .env      # then fill in provider API keys in .env
```
`.env` is gitignored — it is the only place real keys go locally (in prod they
are kubectl-injected, never in the image).

Key knobs (see `.env.example` for the full list):
- `SPLIT_AT_SYMBOLS` — split `/tts/bytes` (one-shot) transcripts into per-
  sentence cache entries (e.g. `.:?`). Empty = off.
- `SPLIT_AT_SYMBOLS_STREAM` — same, for `/tts/stream`.
- `ENABLE_WRITE_THROUGH=true` — store every synth into the cache.
- `CACHE_TTL_*` — length-scaled TTL (longer phrases live longer).

### 2. Install + run (uv)
```bash
uv sync
uv run uvicorn app.main:app --reload
```
Plain venv (no uv): `python3.11 -m venv .venv && source .venv/bin/activate &&
pip install -e . && uvicorn app.main:app --reload`

Server starts on http://127.0.0.1:8000 → try `GET /health`.

## Endpoints
- `POST /tts/bytes` — one-shot synthesis (cached, returns μ-law 8 kHz).
- `POST /tts/stream` — chunked streaming synthesis (raw pcm_s16le @ 16 kHz).
- `POST /cache/clear` — clear the cache at runtime (don't delete `data/` while
  the server runs).
- `GET /health` — liveness + configured providers.
- `GET /stats`, `GET /stats/daily`, `GET /stats/latency` — cache economics.
- `POST /slack-summary` — manually trigger the daily Slack summary.

## Notes
- Cache store lives in `data/` (SQLite db + audio blobs) — gitignored; keep it
  on a persistent volume in prod.
- Split parts are pure-concatenated (no audio is trimmed/cut).

### Speaking-rate control (`params.tempo`, eleven_v3_conversational family)
The v3-conversational models run through the Text-to-Dialogue socket and
support a pitch-preserving speed-up via ffmpeg `atempo`. Three model ids
select the LOCAL processing chain (the suffix is DragonTTS-only — the
upstream call always speaks plain `eleven_v3_conversational`):

| model id | generation rate | chain |
|---|---|---|
| `eleven_v3_conversational` | ElevenLabs' own `pcm_8000` | tempo 1 = served **unaltered**; tempo ≠ 1 = atempo only (no hygiene) |
| `eleven_v3_conversational_tempo` | `ELEVENLABS_V3_NATIVE_SAMPLE_RATE` (44.1 kHz in this `.env`) | atempo, no hygiene; ONE custom sinc downsample to the caller's rate |
| `eleven_v3_conversational_clean_tempo` | same | hygiene + atempo + the same downsample |

- Send `"params": {"tempo": 1.15}` (range 0.5–2.0, else 422). Applies to the
  v3-conversational family ONLY — every other model ignores it and its cache
  key never includes it.
- **`speed` is accepted as an alias**: for these models, a `params.speed`
  value (which TTD would otherwise ignore) is normalized into `tempo` at
  request resolution — so existing clients/templates that already send
  `speed` get the speed-up with ZERO changes. Both spellings share one cache
  entry; an explicit `tempo` wins when both are sent.
- `tempo` 1.0 — absent, default, or explicit — never spawns ffmpeg (pure
  bypass, zero cost). `ELEVENLABS_V3CONV_DEFAULT_TEMPO` (default 1.0) makes a
  non-1.0 value the model default without client changes.
- Kill switches: `ELEVENLABS_TEMPO1_DIRECT_PCM8000=0` makes the BASE model
  run the full-band + hygiene chain (i.e. behave like `_clean_tempo`);
  `ELEVENLABS_ATEMPO_ENABLED=0` disables stretching entirely. If ffmpeg is
  missing from PATH the tempo degrades to passthrough with an error log —
  calls never fail.
- **The cache stores the END RESULT for these models**: the key includes the
  requested output format, and the stored blob is the finished bytes (post
  tempo/hygiene/downsample/encode). A cache HIT is a zero-processing byte
  serve — no ffmpeg, no resample, no re-encode. Batch and streaming produce
  identical stored bytes. Every other model keeps the format-agnostic
  native-store architecture (one entry serves all formats).
- Caveat: a stretched live stream is consumed faster than TTD generates it
  (1.15× → the socket must sustain ≥1.15× realtime or playback stalls);
  ElevenLabs normally delivers in faster-than-realtime bursts, which is why
  the feature ships scoped, opt-in, and kill-switchable.

### Telephony quality: full-band generation + anti-aliased downsample
The `_tempo` / `_clean_tempo` variants (and plain `eleven_v3`, plus the base
model when `ELEVENLABS_TEMPO1_DIRECT_PCM8000=0`) generate AND stretch at
`ELEVENLABS_V3_NATIVE_SAMPLE_RATE` — set it to 44100 (recommended, this
repo's `.env`; probe-confirmed on the TTD socket: 44100 / 24000 / 22050 /
16000 / 8000) for the best 8 kHz call audio. Conversion does one
Hann-windowed-sinc downsample to the caller's rate — so atempo
interpolation noise lands above 4 kHz and is filtered out instead of being
baked into the voice band, and out-of-band speech energy no longer aliases
down (the replaced `audioop.ratecv` interpolation passed a 6 kHz alias at
~94% amplitude; the sinc kernel keeps it under 3%). Pinned by
`tests/test_resample_quality.py`. The classic 16 kHz models get the same
cleaner downsample for free.

Trade-offs vs 8000: ~5.5× websocket bytes per synthesis (the stored cache
blob is the final 8 kHz result, so storage is unchanged), and ~20-70 ms of
off-event-loop CPU per 10 s clip at synth time. Callers still request
μ-law@8000 — nothing client-side changes.

### Presence EQ (spark recovery, full-band variants)
8 kHz band-limiting irreversibly deletes everything above ~3.4 kHz — the
sibilance/air/crispness that reads as "spark". `ELEVENLABS_TELEPHONY_
PRESENCE_BOOST_DB` (default 0 = off) applies a zero-phase raised-cosine
bell peaking at 2.8 kHz (unity below 2.0 / above 3.6 kHz) to the `_tempo`
and `_clean_tempo` chains before the downsample — re-weighting the top
surviving octave so the band-limited voice reads brighter. Try 2.0-3.0 dB;
above ~4 dB turns harsh. It cannot restore what the band limit removed —
the only true fix is a wideband (Opus) transport, which is a caller-side
change. The knob is not in the cache key: clear the cache after changing it.

### Utterance hygiene (v3): dead-air trim + pause cap + silence attenuation
Measured on production v3 generations: ~300 ms average TRAILING silence
per clip (up to 500 ms), ~60-140 ms leading pads, occasional 260-440 ms
internal pauses, and a stationary noise floor only ~22-27 dB under the
speech (audible hiss in every pause). The `_clean_tempo` chain (and plain
`eleven_v3`) cleans BEFORE atempo so the cache stores clean audio.

**Content-line dial** (`ELEVENLABS_HYGIENE_CONTENT_FACTOR`, default 0.15 =
the loud line): frames above `max(p95 × factor, abs floor)` are "actual TTS
data" — never trimmed, capped, or gated; frames below are silence/noise and
get cleaned. The default is the **call-approved chain** (byte-identical
render to the approved comparisons and call recordings). Lower it toward 0.09
(≈ −21 dB) to keep progressively more quiet material — soft tails and quiet
pauses survive (~+100 ms per clip on real takes; every measured v3 noise
floor still cleans at either setting). Word-level speech never approaches
either line, so both ends are speech-safe.
`ELEVENLABS_UTTERANCE_HYGIENE_ENABLED=0` to disable;
`ELEVENLABS_HYGIENE_LEAD_MS` / `ELEVENLABS_HYGIENE_TAIL_MS` /
`ELEVENLABS_HYGIENE_MAX_PAUSE_MS` / `ELEVENLABS_HYGIENE_GATE_FLOOR`
(0 = dead silence, 1.0 = no attenuation) to tune. The live same-format
stream path (pcm_s16le@native requests) is not cleaned — every telephony
request (μ-law) goes through the batch path and is covered.
