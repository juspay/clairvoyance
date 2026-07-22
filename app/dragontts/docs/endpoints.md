# DragonTTS — Endpoints & Curl Reference

Every endpoint, with params, example curls, and sample responses. All curls hit
the port-forwarded service on `localhost:18000` (see Setup).

---

## Setup

### Port-forward (required for every curl)

```bash
kubectl -n beta port-forward deployment/dragontts 18000:8000
```

- Leaves a terminal open. If `18000` is busy (`bind: address already in use`),
  use `18001:8000` (and change the curls) or kill the holder: `fuser -k 18000/tcp`.
- If it won't connect, refresh credentials once:
  ```bash
  gcloud container clusters get-credentials breeze-automatic-mum-01 \
    --region asia-south1 --project breeze-automatic-prod
  ```
- Port-forward is **session-scoped** — it dies when the Cloud Shell tab closes.

### Health

```bash
curl -s http://localhost:18000/health | jq .
```

---

## TTS endpoints

### POST /tts/bytes  — one-shot audio (greetings / batch)

Returns the whole audio body. `X-Cache` header tells the path.

```bash
curl -s -X POST http://localhost:18000/tts/bytes \
  -H 'Content-Type: application/json' \
  -d '{"model_id":"gemini:gemini-3.1-flash-tts-preview","transcript":"hello mate",
       "voice":{"id":"Charon"},"language":"ml",
       "output_format":{"container":"raw","encoding":"mulaw","sample_rate":8000},
       "params":{"style_prompt":"Speak slightly faster than normal while keeping pronunciation clear."}}' \
  -D - -o hellomate.mulaw | grep -i x-cache
```

- `model_id` = `<provider>:<model>` (e.g. `cartesia:sonic-3.5`, `gemini:gemini-3.1-flash-tts-preview`).
- `output_format` is **not** part of the cache key — one entry serves every format.
- `params` holds tuning (`speed`,`volume`,`emotion`,`pitch`,`style_prompt`); **`style_prompt` is part of the key** for gemini.

### POST /tts/stream  — live streaming (the call path)

Chunked stream, low TTFB. Same body shape as `/tts/bytes`; use `output_format`
`pcm_s16le@16000` for live calls.

```bash
curl -s -X POST http://localhost:18000/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"model_id":"cartesia:sonic-3.5","transcript":"hello there",
       "voice":{"id":"bec003e2-3cb3-429c-8468-206a393c67ad"},"language":"en",
       "output_format":{"container":"raw","encoding":"pcm_s16le","sample_rate":16000}}' \
  -o stream.pcm
```

### POST /tts/check  — does a key exist? (+ the resolved key + stored format)

No synth. Body mirrors what Clairvoyance posts.

```bash
curl -s -X POST http://localhost:18000/tts/check -H 'Content-Type: application/json' \
  -d '{"model_id":"gemini:gemini-3.1-flash-tts-preview","transcript":"hello mate",
       "voice":{"id":"Charon"},"language":"ml",
       "output_format":{"container":"raw","encoding":"mulaw","sample_rate":8000},
       "params":{"style_prompt":"Speak slightly faster than normal while keeping pronunciation clear."}}' \
  | jq .
```

Response: `{"cached": true, "key": "024070f7…", "provider": "gemini",
"voice_id": "Charon", "model": "gemini-3.1-flash-tts-preview",
"encoding": "pcm_s16le", "sample_rate": 16000, "size_bytes": …, "hit_count": …}`.

### POST /tts/create  — force-create / override one entry

Synthesizes (or stores `audio_base64`) and caches.

```bash
curl -s -X POST http://localhost:18000/tts/create -H 'Content-Type: application/json' \
  -d '{"model_id":"cartesia:sonic-3.5","transcript":"welcome back",
       "voice":{"id":"bec003e2-3cb3-429c-8468-206a393c67ad"},"language":"en"}' | jq .
```

### POST /tts/create/bulk  — batch warm a phrase library

Capped at `BULK_CREATE_MAX` items per call (HTTP 413 over). Body is a JSON array
of `/tts/create`-shaped requests.

```bash
curl -s -X POST http://localhost:18000/tts/create/bulk -H 'Content-Type: application/json' \
  -d '[{"model_id":"cartesia:sonic-3.5","transcript":"hello","voice":{"id":"v1"},"language":"en"},
       {"model_id":"cartesia:sonic-3.5","transcript":"goodbye","voice":{"id":"v1"},"language":"en"}]' | jq .
```

### POST /tts/delete  — delete by request body (resolves the key)

Same body as `/tts/check`; deletes the entry that body resolves to.

```bash
curl -s -X POST http://localhost:18000/tts/delete -H 'Content-Type: application/json' \
  -d '{...same body as check...}' | jq .
```

---

## Cache admin endpoints

### GET /cache  — paginated listing with text/date filters

```bash
# newest 1000 (cap is 1000/call), text + provider/voice filters
curl -s 'http://localhost:18000/cache?limit=1000&offset=0' | jq '.entries | length'
curl -s 'http://localhost:18000/cache?provider=gemini&limit=1000' | jq '.entries[].text'

# search by text — exact (default) or substring; date filters (YYYY-MM-DD)
curl -s 'http://localhost:18000/cache?q=hello&match=substring&limit=1000' | jq
curl -s 'http://localhost:18000/cache?created_after=2026-07-10&limit=1000' | jq
curl -s 'http://localhost:18000/cache?created_after=2026-07-09&created_before=2026-07-10&limit=1000' | jq
```

- `match=exact` (default) or `substring`. Substring escapes `%`/`_`/`\` so they
  match literally (not as LIKE wildcards).
- `created_after` is **inclusive** of that day; `created_before` is **exclusive**
  (strictly before that day's start).
- Just the text: `curl -s '.../cache?limit=1000' | jq -r '.entries[].text'`.

### GET /cache/{key}  — fetch a cached clip's audio (raw bytes)

```bash
KEY=e11fb3021d50d0f08df1b23c034743c4ef7c60a1fae98bb2c0d97559830572a1
curl -s -o phrase.raw http://localhost:18000/cache/$KEY
```

Raw bytes (no header). Wrap a WAV to play (16 kHz mono s16):
```bash
python3 -c "import wave; w=wave.open('phrase.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(open('phrase.raw','rb').read()); w.close()"
```
Cloud Shell has no audio — serve a subfolder (never `~`) and use Web Preview:
```bash
mkdir -p ~/out && cp phrase.wav ~/out/ && cd ~/out && python3 -m http.server 8080
```

### DELETE /cache/{key}  — delete one entry by hash

```bash
curl -s -X DELETE http://localhost:18000/cache/$KEY | jq .
# -> {"status":"deleted","key":"e11fb3…"}
```

### POST /cache/clear  — bulk delete (+ dry-run preview)

```bash
# preview count/bytes/providers without deleting:
curl -s -X POST 'http://localhost:18000/cache/clear?dry_run=true' | jq
curl -s -X POST 'http://localhost:18000/cache/clear?provider=gemini&dry_run=true' | jq
# actually clear:
curl -s -X POST 'http://localhost:18000/cache/clear?provider=gemini' | jq
curl -s -X POST 'http://localhost:18000/cache/clear' | jq   # EVERYTHING
```

### POST /cache/delete-by-text  — delete by text (exact/substring)

Body: `{text, provider?, voice_id?, match, dry_run}`. **dry_run defaults `true`**
(preview). **Requires ≥1 filter** (text/provider/voice_id) — empty body → 400
(use `/cache/clear` to wipe everything).

```bash
# preview (dry_run defaults true):
curl -s -X POST http://localhost:18000/cache/delete-by-text -H 'Content-Type: application/json' \
  -d '{"text":"hello","match":"substring"}' | jq
# delete:
curl -s -X POST http://localhost:18000/cache/delete-by-text -H 'Content-Type: application/json' \
  -d '{"text":"hello","match":"substring","dry_run":false}' | jq
# one provider only:
curl -s -X POST http://localhost:18000/cache/delete-by-text -H 'Content-Type: application/json' \
  -d '{"text":"ഹലോ","match":"substring","provider":"gemini","dry_run":false}' | jq
```

Response: `{"matched": N, "deleted": N, "dry_run": bool,
"entries": [{key, provider, voice_id, text}, …]}`.

### POST /cache/delete-by-age  — evict entries older than N days

**dry_run defaults `true`.**

```bash
curl -s -X POST 'http://localhost:18000/cache/delete-by-age?older_than_days=7' | jq            # preview
curl -s -X POST 'http://localhost:18000/cache/delete-by-age?older_than_days=7&dry_run=false' | jq
```

### POST /cache/backfill-ttl  — one-shot: TTL the legacy entries

Gives every NULL-TTL (pre-feature) row a random 48–72h expiry so they age out
instead of living forever. **Idempotent.** Run once after deploying the TTL feature.

```bash
curl -s -X POST http://localhost:18000/cache/backfill-ttl | jq
# -> {"status":"backfilled","updated":1287}
```

---

## Analytics endpoints

### GET /stats  — aggregate metrics + cache snapshot + latency (date-filterable)

```bash
curl -s 'http://localhost:18000/stats' | jq '{hit_rate, requests, hits, misses,
  words_served, words_synthesized,
  words_from_cache_pct: (if .words_served>0 then ((.words_served-.words_synthesized)*100/.words_served|floor) else null end)}'

# one day:
curl -s 'http://localhost:18000/stats?from=2026-07-09&to=2026-07-09' | jq

# cache snapshot (entries/bytes/per-provider):
curl -s 'http://localhost:18000/stats' | jq '{phrases:.entries, total_mb:((.total_bytes/1048576*100|floor)/100),
  by_provider:(.by_provider|to_entries|map({provider:.key,phrases:.value.entries,mb:((.value.total_bytes/1048576*100|floor)/100)}))}'

# per-provider rollup for a day:
curl -s 'http://localhost:18000/stats?from=2026-07-09&to=2026-07-09' | jq '.providers'
```

### GET /stats/daily  — extensive day-wise analytics (per day × per provider)

```bash
curl -s 'http://localhost:18000/stats/daily?from=2026-07-10&to=2026-07-14' | jq
curl -s 'http://localhost:18000/stats/daily?from=2026-07-10&to=2026-07-14&provider=gemini' | jq
```

Each day: `totals` (requests, hits, misses, hit_rate, synth_calls, words_served,
words_synthesized, words_from_cache_pct, stitch_calls, stitch_words_assembled,
stitch_words_synthesized, stitch_coverage_avg, bytes_served, creates, deletes)
**and** `by_provider` (per-provider: requests, hits, misses, hit_rate, synth_calls,
bytes_served, words_served). `provider=` narrows totals to that provider.

> Per-provider **latency** and per-provider **words_from_cache_pct** aren't
> available (the store has no `provider` column in `latency_samples` / no
> per-provider `words_synthesized`).

---

## X-Cache response header

Every TTS response carries `X-Cache`:

| Value | Meaning |
|---|---|
| `HIT` | exact-match cache serve |
| `MISS` | no usable cache; full provider synth |
| `MISS-STITCH` | full-text miss assembled from cached fragments + synthesized gaps |
| `MISS-STITCH-PASSTHROUGH` | stitch MISS streamed prefix-first (TTFB ≈ 0) |

```bash
kubectl -n beta logs deployment/dragontts --tail=5000 \
 | grep -oE 'CACHE (HIT|MISS|MISS-STITCH|MISS-STITCH-PASSTHROUGH)' | sort | uniq -c
```

---

## Environment variables (deploy knobs)

Set in `deploy/k8s/deployment.yaml`. Non-secret — safe to commit.

### TTL (new)
| Env | Prod | Purpose |
|---|---|---|
| `CACHE_TTL_BASE_SECONDS` | `172800` (48h) | min TTL for any phrase |
| `CACHE_TTL_PER_WORD_SECONDS` | `21600` (+6h) | added per word |
| `CACHE_TTL_MAX_SECONDS` | `518400` (6d) | cap |
| `TTL_PURGE_INTERVAL_SECONDS` | `1200` (20m) | purge sweep cadence |
| `CACHE_TTL_BACKFILL_MIN_HOURS` | `48` | backfill random-TTL lower bound |
| `CACHE_TTL_BACKFILL_MAX_HOURS` | `72` | backfill random-TTL upper bound |

### Cache policy
| Env | Prod | Purpose |
|---|---|---|
| `ENABLE_WRITE_THROUGH` | `true` | store MISS/STITCH results on the request path |
| `MAX_CACHE_BYTES` | `0` | 0 = unlimited |

### Performance / resilience
| Env | Prod | Purpose |
|---|---|---|
| `THREAD_POOL_WORKERS` | `32` | blocking-work pool per worker |
| `BULK_CREATE_MAX` | `100` | cap on `/tts/create/bulk` items/call |
| `PROVIDER_RESILIENCE` | `{...max_concurrent:8...}` | per-provider bulkhead |

### TTS text normalization
| Env | Prod | Purpose |
|---|---|---|
| `TTS_NORMALIZE_NUMBERS` | `false` | expand digits→words in key + synth text |
| `TTS_LEADING_DOT` | `true` | ElevenLabs leading-dot hint (synth only, not key) |

### Predictive warmer / stitch (measurement phase)
| Env | Recommended now | Purpose |
|---|---|---|
| `PREDICTIVE_WARM_ENABLED` | `false` | redundant when write-through ON; turn back on if stitch re-enabled |
| `PREDICTIVE_STITCH_ENABLED` | `false` (Phase 1) | OFF for the write-through+TTL baseline; ON w/ conservative coverage later |
| `PREDICTIVE_STITCH_MIN_COVERAGE` | `0.5` (→ `0.65` if re-enabled) | cached fraction required to stitch |
| `ENABLE_PASS_THROUGH_STITCH` | `true` | prefix-first streaming (needs stitch ON) |

### Metrics
| Env | Prod | Purpose |
|---|---|---|
| `METRICS_LATENCY_SAMPLE_RATE` | `0.3` | fraction of requests timed |
| `METRICS_LATENCY_RETENTION_DAYS` | `14` | prune `latency_samples` older than this |

> **Credentials are never in the manifest** — provider keys are injected at
> deploy time (`kubectl`).

---

## Deploy & operate

```bash
# roll a new image
kubectl -n beta rollout restart deployment/dragontts
# port-forward
kubectl -n beta port-forward deployment/dragontts 18000:8000
# PVC utilization (audio + DB + slack)
kubectl -n beta exec deployment/dragontts -- sh -c 'du -sh /app/data /app/data/blobs /app/data/dragontts.db; df -h /app/data'
```

### First-deploy workflow for the TTL feature
1. Roll the new image (`rollout restart`).
2. Port-forward.
3. **Backfill legacy entries once:** `curl -s -X POST http://localhost:18000/cache/backfill-ttl | jq`
4. Watch ~24–48h: `/stats` cache size + `df -h /app/data`, and
   `kubectl ... logs ... | grep "TTL purge"` to confirm sweeps run.

---

## Safety notes

- **Always clear/delete via the API** — never delete DB/blob files while the pod runs.
- **`delete-by-text`/`delete-by-age` default to `dry_run=true`** — preview first.
  `/cache/clear` defaults to `dry_run=false` (opt in to preview).
- **Don't `python3 -m http.server` from `~`** in Cloud Shell (homes hold secrets);
  serve a dedicated subfolder.
- **Dates are UTC** — `metrics_daily` / `latency_samples` store UTC dates.
