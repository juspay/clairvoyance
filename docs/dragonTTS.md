# DragonTTS — Technical Architecture

> Cached text-to-speech proxy. Serves repeated synthesis from a local cache to
> cut latency and cost; falls back to a live provider on a miss and tees the
> result back into the cache. Multi-provider (Cartesia / ElevenLabs / Sarvam /
> Gemini), FastAPI, SQLite + filesystem blobs on a persistent volume.
>
> **Path convention:** code paths are relative to the DragonTTS app root
> (`clairvoyance/dragontts/`) — so `app/main.py` = `clairvoyance/dragontts/app/main.py`.
> `endpoints.md`, `dragontts-scaling-and-isolation.md`, and `deploy/k8s/deployment.yaml`
> live in the **standalone dragonTTS repo**. This doc is the *how it works inside* reference.

---

## 1. At a glance

| | |
|---|---|
| **Role** | TTS caching proxy in front of Cartesia / ElevenLabs / Sarvam / Gemini |
| **Stack** | FastAPI + uvicorn (4 workers, uvloop), SQLite (metadata) + filesystem (audio blobs) |
| **Storage** | SQLite DB + sharded blob dir on a **RWO PVC** (single pod, zonal disk) |
| **Cache policy (prod)** | **write-through ON** |
| **Native audio** | `pcm_s16le` @ 16 kHz for every provider (μ-law 8 kHz produced on serve) |
| **Primary caller** | Clairvoyance (`enable_tts_caching` templates) |
| **Deploy** | GKE, dedicated `dragontts-pool` (e2-standard-4), 100 Gi PVC, single replica |

> **Single pod, zero user-facing downtime** — on restart, clairvoyance falls back
> to its upstream TTS while DragonTTS drains and recycles (§6). **One 100 Gi PVC**
> holds the whole cache; no DB server or object store to operate (§3). **Cache
> HIT ≈ ~1 ms**; a MISS pays only the provider synth time (warm sockets keep
> time-to-first-byte ~100–300 ms) — see §2.

Two read paths, both cache-backed:
- **`POST /tts/bytes`** — one-shot: returns the full clip (HIT serves cached bytes; MISS synthesizes, stores via write-through, returns).
- **`POST /tts/stream`** — low-TTFB streaming: MISS streams provider chunks live **and** tees them into the cache; HIT streams the cached blob.

---

## 2. Request flow

```
                ┌──────────── /tts/bytes (one-shot) ────────────┐
caller ───────► │  derive key → cache lookup                     │
                │    HIT  → serve cached bytes (convert-on-serve)│ ─────► caller
                │    MISS → synth (provider) → write-through     │
                │          → store blob+row → serve              │
                └────────────────────────────────────────────────┘

                ┌──────────── /tts/stream (low TTFB) ────────────┐
caller ───────► │  derive key → cache lookup                     │
                │    HIT  → stream cached blob                   │ ─────► caller
                │    MISS → stream provider chunks live (tee)    │
                │          → accumulate → store blob+row on done  │
                └────────────────────────────────────────────────┘
```

- **Single-flight:** an in-process `_inflight` future map de-dupes identical MISSes — concurrent identical requests share one synth, not N.
- **Resilience:** per-provider bulkhead (concurrency cap + optional rate limit); provider HTTP/network failures map to **502/503, never 500**.

### Latency profile
The cache exists to make the common case (a repeat phrase) effectively instant,
and to keep even a MISS cheap:

| Path | Latency | Why |
|---|---|---|
| **HIT** (`/tts/bytes` or `/tts/stream`) | **~1 ms** | A blob read (served from the OS page cache when hot) + on-serve format conversion. No synth, no provider network call. |
| **MISS → first audio byte** (TTFB, `/tts/stream`) | **~100–300 ms** | The provider **socket is already warm** (pre-connected pool), so there's no TLS/handshake per request — only the provider's own time to start generating. |
| **MISS → full clip** | **~0.4–1.5 s** | Just the provider's synth time for the phrase (varies by provider + length); the result is teed into the cache as it streams, so the *next* request for it is a ~1 ms HIT. |

So a MISS adds only the **synthesis** time, not connection setup — the warm pools
are what keep TTFB low — and every MISS converts the phrase into an ~1 ms HIT
for all future calls (same or later conversations).

---

## 3. Storage layer (SQLite + filesystem on RWO PVC)

### Two stores, one volume
- **`SQLiteMetadataStore`** (`data/dragontts.db`) — cache keys → blob paths, hit counts, TTLs, request/latency analytics rollups.
- **`FilesystemBlobStore`** (`data/blobs`, content-addressed + sharded `ab/cd/<key>`) — the audio bytes.

Both live on the same **RWO (ReadWriteOnce) PVC** — a zonal GKE persistent disk that mounts to exactly one node/pod.

### Why this storage: SQLite + filesystem on an RWO PVC
A single-pod cache service wants the **simplest durable store** — an embedded DB
+ a filesystem on a persistent disk.

**Why a PVC (persistent volume):**
- **Survives restarts** — the cache persists across pod restarts and deploys, so a warm cache comes back instead of cold-starting (and re-paying for synth) on every rollout.
- **Cheap + fast** — a zonal persistent disk is inexpensive *block* storage with the IOPS/latency SQLite wants; no network hop, no separate service to run.
- **One volume, two stores** — the SQLite DB and the blob dir share one mount; nothing else to provision, operate, or bill.

**Why SQLite + filesystem:**
- **Embedded** — no DB server process to run, tune, or pay for; the metadata store is just a file on the volume, transactional and fast for this read-heavy cache workload.
- **Hot reads via the OS page cache** — frequently-served blobs come straight from kernel memory, which is why a HIT is ~1 ms (no app-level cache needed).
- **Atomic writes** — the blob store writes each blob via **temp file + `fsync` + `os.replace`** (atomic rename), so a reader never sees a half-written blob, and the blob is durable before the metadata row that references it commits.

> **Single disk, single zone:** the PVC attaches to one node in one zone (the
> disk's zone is load-bearing — see `dragontts-scaling-and-isolation.md`). A
> **cache doesn't need HA** — its *caller* (clairvoyance) tolerates a DragonTTS
> restart via upstream fallback (§6), so the single-pod / single-disk choice
> costs no user-facing downtime.

### Native format + convert-on-serve
Every provider synthesizes to **`pcm_s16le` @ 16 kHz** (ElevenLabs `output_format=pcm_16000`; Gemini emits 24 kHz and is resampled 24k→16k). The cache key is **format-agnostic**, so one native entry serves every requested format: a telephony caller asking for μ-law 8 kHz gets the 16 kHz PCM entry converted on serve (`app/audio/format.py`). This keeps the cache small and format-neutral.

---

## 4. Caching

### Write-through (ON in prod)
Every MISS synthesizes, stores (blob + row), then returns. There is **no frequency gate** — every unique phrase is cached on first miss. Combined with length-scaled TTL, the cache self-bounds by time.

### Cache key
Derived (in `app/cache/key.py`) from: `provider:model`, `voice_id`, `language`, `output_format`, **canonical params**, and normalized text. `canonical_params` is a sorted-JSON of the non-default tuning params, so different speeds / SSML-on-vs-off / pitches get **distinct keys** (no collision, no stale wrong-rate audio).

### Length-scaled TTL
Every stored entry gets a TTL that scales with phrase length — longer phrases
cost more to re-synthesize, so they're preserved longer; short ones age out
faster. There is no "permanent" tier.

```
ttl(words) = clamp(BASE + PER_WORD × words, BASE, MAX)

BASE     = 172800s   (48h floor — min TTL for any phrase)
PER_WORD = 21600s    (+6h per word)
MAX      = 518400s   (6d cap)
```

Worked examples: 1 word → 54h · 5 words → 78h · 10 words → 108h · 16+ words → 144h (6d, capped).

Env knobs (`CACHE_TTL_*`):
- `CACHE_TTL_BASE_SECONDS` (48h) — floor / minimum TTL.
- `CACHE_TTL_PER_WORD_SECONDS` (6h) — added per word.
- `CACHE_TTL_MAX_SECONDS` (6d) — hard cap.
- `TTL_PURGE_INTERVAL_SECONDS` (20 min) — cadence of the purge sweep.
- Set `CACHE_TTL_BASE_SECONDS ≤ 0` to **disable TTL entirely** (entries never expire; no purge runs).

A background purge loop deletes expired **rows + their blob files** every
`TTL_PURGE_INTERVAL_SECONDS` (default 20 min). Pre-existing rows with no TTL
(created before this feature, under the legacy flat `TTL_SECONDS=0` knob — now
superseded and unused) are handled by `POST /cache/backfill-ttl`: a one-shot,
idempotent op that assigns each NULL-TTL row a randomized 48–72h expiry so they
age out gradually (randomized, not flat, to avoid a synchronized re-synth spike).

> Note: `MAX_CACHE_BYTES` (default `0` = unlimited) exists in config but is
> **not enforced** by any eviction loop today — cache growth is bounded by TTL,
> not by size.

---

## 5. Providers & warm pools

| Provider | Transport (synth / stream) | Native | Default model | Tuning params |
|---|---|---|---|---|
| **Cartesia** | HTTP `/tts/bytes` / **WS pool** (context multiplex) | pcm 16k | `sonic-3.5` | `speed`, `volume`, `emotion` |
| **ElevenLabs** | HTTP `/v1/text-to-speech/{voice}` / **multi-context WS pool** | pcm 16k (`pcm_16000`) | `eleven_flash_v2_5` | `speed`, `enable_ssml_parsing` |
| **Sarvam** | HTTP `/text-to-speech` / **WS pool** (LIFO, not multiplexed) | pcm 16k | `bulbul:v3` | `speed`(→pace), `pitch` |
| **Gemini** | gRPC `streaming_synthesize` (24k→16k resample) | pcm 16k | `gemini-3.1-flash-tts-preview` | `style_prompt` |

### Warm socket pools (low TTFB on stream misses)
Streaming misses reuse **persistent, pre-warmed sockets** instead of a fresh handshake each time:
- **Cartesia** — one socket multiplexes many utterances by `context_id`.
- **ElevenLabs** — multi-context WS, pooled **per `(voice_id, model_id, enable_ssml_parsing)`**; up to **5 concurrent contexts per socket** (server-enforced). `enable_ssml_parsing` is a **connect-time** socket setting (an SSML-on socket parses `<break/>` for *all* its utterances), so on/off can't share a socket.
- **Sarvam** — WS, **not** multiplexed (one utterance per socket at a time); a LIFO stack of warm connections.
- **Gemini** — process-cached gRPC channel; cancels the stream on early abandon so HTTP/2 concurrency isn't exhausted.

Only the **default** voice/model is pre-warmed at startup; other voices warm lazily on their first streaming miss, then stay warm (auto-reconnect on drop). Pool sizes default to 2 each (`*_stream_pool_size`).

### Param forwarding (clairvoyance → DragonTTS)
Clairvoyance's `tts_configuration` carries the tuning knobs; its DragonTTS client (`_collect_params`) forwards the **6 param fields** into the request `params` dict: `speed, volume, emotion, pitch, style_prompt, enable_ssml_parsing`. DragonTTS applies the ones relevant to the nested provider and folds all of them into the cache key. (`language` is a top-level request field, not a param.)

### SSML (ElevenLabs)
`enable_ssml_parsing=true` makes ElevenLabs honor `<break time="Ns"/>` etc. as real pauses instead of reading the tags aloud. DragonTTS honors it on **both** paths:
- **`/tts/stream`** — selects the SSML warm socket (connect-URI query param).
- **`/tts/bytes`** — sends `enable_ssml_parsing: true` in the HTTP body, **and** routes the synth through the WS stream path (smoother render) before caching.
SSML-on gets its own cache key (distinct from SSML-off/absent), so an SSML HIT never serves a plain-text render.

---

## 6. Graceful shutdown & kill switch

Goal: when a pod is terminating, **(1) stop new traffic first, (2) let in-flight requests finish, (3) then die** — no cut streams, no requests routed to a dying pod.

### The drain sequence
```
k8s preStop  ──►  GET /drain
                   1. enable kill switch  (mark draining → /ready 503  +  POST clairvoyance bypass)
                   2. send Slack alert    (🛑 kill switch activated, awaited ≤2s)
k8s SIGTERM  ──►  uvicorn stops accepting; drains in-flight HTTP/stream
                   (≤ --timeout-graceful-shutdown 100s in the Dockerfile — the OPERATIVE drain)
                 lifespan shutdown (every worker):
                   3. wait_for_inflight_drain  (backstop — normally instant, since uvicorn
                      already drained; capped at GRACEFUL_DRAIN_MAX_SECONDS 120s)
                   4. cleanup             (flush metrics, WAL checkpoint, close pools/DB)
```

**Two-layer drain timeout.** uvicorn's `--timeout-graceful-shutdown=100` (Dockerfile) is the budget that actually protects in-flight requests — it runs *first*, waiting up to **100s** for live HTTP/stream tasks to finish before lifespan shutdown is even called. `GRACEFUL_DRAIN_MAX_SECONDS=120` is a *backstop* in the lifespan phase: by then uvicorn has already drained the HTTP tasks the ASGI gauge tracks, so `wait_for_inflight_drain` normally returns immediately (inflight≈0); its 120s ceiling only binds for work uvicorn doesn't account for. So `terminationGracePeriodSeconds` must exceed **100s (uvicorn) + preStop (~8s) + lifespan cleanup (~2s) ≈ 110s** — **150 is comfortable headroom**. (The 120 backstop is intentionally ≥ uvicorn's 100 so the lifespan phase is never the binding constraint.)

- **`/drain`** (`app/api/v1/health.py`) is the preStop hook. It is **idempotent** and gated by `ENABLE_GRACEFUL_DRAIN`.
- **`/ready`** returns **503 while draining** (so k8s stops routing new traffic); **`/health` stays 200** (liveness — don't restart the pod mid-drain).
- **Kill switch** (`app/drain.py` `notify_clairvoyance`): POSTs `{"action":"kill_switch"}` to clairvoyance's admin endpoint (`/agent/voice/breeze-buddy/admin/dragontts/manage`) with `Authorization: Bearer <JWT>`. **One-way** — DragonTTS only ever engages the bypass; **restore is a manual operator action** on clairvoyance (it does NOT auto-restore on startup).

### Why the inflight gauge is a pure-ASGI middleware
The in-flight counter **must** stay >0 for the entire lifetime of a streamed response. `@app.middleware("http")` (Starlette's `BaseHTTPMiddleware`) **can't** do this — its dispatch returns *after* the response object is built but *before* a `StreamingResponse` streams its body, so a `finally: decr()` fires mid-stream and the gauge hits 0 prematurely → shutdown proceeds → streams get cut.

`InflightTrackingMiddleware` (`app/main.py`) is a **pure-ASGI** middleware that wraps `send` and decrements **only on the terminal ASGI message** (`http.response.body` with `more_body=false`) or `http.disconnect`, with a `done` guard + `finally` for exactly-once decrement. Verified: gauge = 1 during a stream, 0 only after.

### Clairvoyance consumer-side kill switch (the real gate)
Clairvoyance independently watches DragonTTS: a scheduler task probes `/health` (~60s); on failure it one-way marks a Redis flag `dragontts:health = "0"`, and `enable_tts_caching` templates then **bypass DragonTTS and use their upstream provider directly** (no user-facing silence). DragonTTS's `/drain` flip is the *instant* version of this (no 60s lag); the monitor is the backstop.

### ✅ Zero-downtime despite a single-pod architecture
DragonTTS runs as **one replica** — yet a pod restart causes **no user-facing
TTS downtime**. The trick isn't HA at the DragonTTS layer; it's **graceful
degradation at its caller** (clairvoyance):

1. **Before the pod dies**, `/drain` flips the clairvoyance kill switch →
   clairvoyance routes `enable_tts_caching` TTS to its **upstream provider
   directly**, bypassing DragonTTS entirely.
2. **In-flight** DragonTTS requests drain to completion (ASGI inflight gauge +
   `wait_for_inflight_drain`), so no live stream is cut.
3. **During the restart window**, clairvoyance keeps serving TTS from upstream —
   callers still get audio (just uncached: marginally higher latency/cost, **no
   silence, no errors**).
4. **New pod up** → operator restores the flag → caching resumes.

So the single-pod choice buys simplicity **without** trading away availability:
the blast radius of a DragonTTS restart is a *temporary cache bypass*, never a
TTS outage.

### ⚠️ Wiring required in the Deployment (operational)
The code is inert until the manifest wires it. `deploy/k8s/deployment.yaml` needs (applied via kubectl — creds are never baked into the image):
```yaml
terminationGracePeriodSeconds: 150       # > uvicorn --timeout-graceful-shutdown (100) + preStop ~8s + lifespan cleanup
lifecycle:
  preStop:
    httpGet: {path: /drain, port: 8000}
livenessProbe:  {httpGet: {path: /health, port: 8000}}
readinessProbe: {httpGet: {path: /ready,  port: 8000}}
env:
  - {name: CLAIRVOYANCE_URL,       value: ...}   # kubectl-injected
  - {name: CLAIRVOYANCE_JWT_TOKEN, value: ...}   # long-lived admin JWT (watch expiry!)
```
Until these are applied, `/drain`, `/ready`, and `graceful_drain` do nothing in prod.

---

## 7. Resilience

- **Per-provider bulkhead** (`provider_max_concurrent_synths`, default 24): caps in-flight synth/stream calls per provider so a slow/hung provider can't starve the others. Over-budget calls wait up to `provider_bulkhead_wait_timeout_ms` (2500ms) then **503** (with `Retry-After`). Per-provider overrides via `PROVIDER_RESILIENCE` JSON env.
- **Error mapping**: provider `HTTPStatusError`/`ProviderError` → **502**; connection errors → **503**. Never a bare 500.
- **Stream fallback**: ElevenLabs WS stream falls back to one-shot HTTP synth only if nothing has streamed yet (can't safely fall back mid-stream).

---

## 8. Metrics & observability

- **Write-behind metrics** (`metrics_write_behind_enabled`): HIT touch/metric updates are batched + flushed every 500ms / 64 items (and on graceful shutdown) so a HIT returns audio without awaiting a SQLite write.
- **Latency sampling** (`metrics_latency_sample_rate`, default 0.3): a fraction of requests are timed (synth, total, cache-serve, TTFB) for the avg/p95 rollup; rows pruned after `metrics_latency_retention_days` (14).
- **Endpoints**: `/stats` (cache economics + session), `/stats/daily` (day-wise), `/stats/latency` (per-provider/day avg-p95 + derived `miss_overhead_us`).
- **Slack daily summary**: once/day at `slack_summary_time_utc` (16:30 UTC = 10 PM IST) — hit rate, words-from-cache %, est. cost saved (INR), PVC usage. Manual trigger: `POST /slack-summary`. Webhook absent ⇒ feature off.

---

## 9. Memory management

- **`MALLOC_ARENA_MAX=4`** (Dockerfile): caps glibc malloc arenas so freed audio/resample buffers return to the OS instead of fragmenting across ~100 arenas (the RSS-creep cause on a 4-worker pod).
- **Periodic `malloc_trim`** (`malloc_trim_interval_seconds`, default 300): each worker runs `gc.collect()` + `malloc_trim(0)` to hand freed heap back to the OS. No effect on audio quality or concurrency — it only releases already-freed memory. Skipped silently on non-glibc (musl).
- **WAL checkpoint**: a loop checkpoints the SQLite `-wal` file every 5 min (and on shutdown) so it stays bounded while worker connections are held open.

---

## 10. Deployment / infra

- **Cluster**: GKE `breeze-automatic-mum-01` (asia-south1), namespace `beta`, deployment `dragontts`, **single replica**.
- **Node pool**: dedicated `dragontts-pool` (e2-standard-4, 4 vCPU/16 GB, CPU limit 3000m), isolated from clairvoyance/redis. **Zonal** — the PVC's zone is load-bearing (a new pool must land in the same zone or the disk won't attach). See `dragontts-scaling-and-isolation.md`.
- **PVC**: 100 Gi RWO persistent disk → `data/` (SQLite db + blobs). Don't delete `data/` while the server runs; clear via `POST /cache/clear`.
- **Image**: built/pushed by `.github/workflows/dragontts-docker-publish.yml` (context `dragontts/`, own `Dockerfile`/`pyproject`/`uv.lock`) to `asia-south1-docker.pkg.dev/breeze-automatic-prod/dragontts/dragontts`. Clairvoyance's `.dockerignore` excludes `dragontts/` so it never ships in the clairvoyance image.
- **Secrets**: provider keys + `CLAIRVOYANCE_JWT_TOKEN` are **kubectl-injected in prod**, never in the image or `deployment.yaml`.

### Run locally
```bash
cp .env.example .env        # fill provider keys
uv sync
uv run uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

### Port-forward to prod (for curls — see `endpoints.md`)
```bash
kubectl -n beta port-forward deployment/dragontts 18000:8000
```

---

## 11. Configuration reference (key envs)

| Env | Default | Purpose |
|---|---|---|
| `ENABLE_WRITE_THROUGH` | `true` | store every MISS into the cache |
| `CACHE_TTL_BASE_SECONDS` | `172800` | length-scaled TTL floor (48h) |
| `CACHE_TTL_PER_WORD_SECONDS` | `21600` | +TTL per word |
| `CACHE_TTL_MAX_SECONDS` | `518400` | TTL cap (6d) |
| `TTL_PURGE_INTERVAL_SECONDS` | `1200` | expired-entry purge cadence |
| `SPLIT_AT_SYMBOLS` / `_STREAM` | `""` | per-sentence split (OFF) |
| `*_STREAM_POOL_SIZE` | `2` | warm socket pool size per provider |
| `MALLOC_TRIM_ENABLED` / `_INTERVAL_SECONDS` | `true` / `300` | RSS-creep mitigation |
| `ENABLE_GRACEFUL_DRAIN` | `true` | master switch for the drain flow |
| `--timeout-graceful-shutdown` *(Dockerfile)* | `100` | **operative** in-flight HTTP/stream drain after SIGTERM (runs *before* lifespan) |
| `GRACEFUL_DRAIN_MAX_SECONDS` | `120` | lifespan-phase drain *backstop* — normally instant (uvicorn already drained); ceiling only binds for work uvicorn doesn't track |
| `CLAIRVOYANCE_URL` / `CLAIRVOYANCE_JWT_TOKEN` | `""` | kill-switch target + admin JWT (kubectl-injected) |
| `CLAIRVOYANCE_KILL_SWITCH_TIMEOUT` | `5.0` | kill-switch POST timeout |
| `SLACK_WEBHOOK_URL` | `""` | daily summary + kill-switch alerts (off if empty) |

---

## 12. Related docs
- `endpoints.md` — full API + curl reference.
- `dragontts-scaling-and-isolation.md` — dedicated node-pool + PVC migration runbook.
- Clairvoyance side: the kill-switch consumer (`dragontts:health` flag) + admin endpoint live in `clairvoyance/app/ai/voice/agents/breeze_buddy/tts/dragontts/`.
