# dragonTTS

TTS caching service — serves cached text-to-speech audio to cut latency and cost
on repeated synthesis. FastAPI app, SQLite + filesystem store, multi-provider
(Cartesia / ElevenLabs / Sarvam / Gemini).

## Run locally

The folder is self-contained: a venv, the deps, and the `app` package all live
inside it. **Always `cd` into this folder and run from here** — even when it sits
nested inside another repo (e.g. as `clairvoyance/dragontts/`). Git boundaries
don't matter to Python; only the working directory + the venv do.

### 1. Configure secrets

```bash
cp .env.example .env      # then fill in provider API keys in .env
```

`.env` is gitignored — it never gets committed. It's the ONLY place real keys go
locally (in prod they're kubectl-injected, never in the image).

### 2a. With `uv` (this repo's tool — matches the Dockerfile / `uv.lock`)

```bash
uv sync                   # creates .venv and installs exactly the locked deps
uv run uvicorn app.main:app --reload
```

### 2b. With plain Python (no uv)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Server starts on http://127.0.0.1:8000 — try `GET /health`.

### Nested inside another repo (e.g. clairvoyance)

- **Use a separate venv** inside this folder (the steps above do that) — don't
  reuse the parent repo's venv; deps differ and would clash.
- Run from **inside this folder**, so `import app.main` resolves to *this*
  `app/` (the parent repo's own `app/` package, if any, is never on the path).

## Operation

- `POST /tts/bytes` — one-shot synthesis (cached).
- `POST /tts/stream` — chunked streaming synthesis.
- `POST /cache/clear` — clear the cache at runtime (don't delete `data/` while
  the server runs).
- `GET /health`, `GET /stats` — liveness + cache economics.

See `docs/endpoints.md` for the full API and `docs/overview.html` for architecture.
