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
