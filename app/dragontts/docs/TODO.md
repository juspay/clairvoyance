# DragonTTS — TODO / backlog

Deferred improvements, captured so they're not lost. Not urgent — pick up when
convenient. Add new items below.

---

## Flatten the startup CPU spike on rollout

**Status:** deferred (acceptable as-is; revisit if it bothers anyone or before
scaling traffic up).

**Symptom:** a transient CPU spike right after a new image rolls out (pod restart).

**Root cause:** startup lifespan work runs **once per uvicorn worker**, and the
`Dockerfile` runs `--workers 4` — so every CPU-heavy startup job fires 4×
concurrently at boot:
- **WS pool warming** — Cartesia (4 sockets) + ElevenLabs (2) per worker =
  ~24 TLS/WSS handshakes (crypto-heavy) bunched at startup.
- **`reconcile_blobs()`** — `os.walk()` over the entire cache blob dir + a DB
  lookup, per worker. Cost grows with cache size.
- **heavy-lib imports** — numpy, google-cloud-texttospeech (gRPC), httpx,
  pydantic, per worker.

The spike is transient (seconds) and settles once startup completes. It is not
steady-state load.

### Fix 1 (biggest lever) — reduce workers 4 → 2

In `Dockerfile`:
```dockerfile
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--loop", "uvloop", "--no-access-log", "--limit-concurrency", "1024"]
```
Halves the concurrent startup work. Safe for current load (~0.1–0.3 req/s
average, peaks a few/sec) — each worker has uvloop + a 32-thread pool for
blocking IO, so 2 workers is ample headroom. The Slack once-per-day claim
dedupes across any worker count, so that feature is unaffected. Revisit if
traffic climbs meaningfully.

### Fix 2 — defer `reconcile_blobs()` to a background task

In `app/main.py` lifespan (currently `await cache.reconcile_blobs()`):
```python
asyncio.create_task(cache.reconcile_blobs())   # was: await cache.reconcile_blobs()
```
Removes the full blob-dir walk from the boot critical path — the pod reaches
"ready" faster and the walk spreads out afterward instead of spiking. Reconcile
is best-effort orphan cleanup, so deferring it a few seconds is harmless. Keep
the existing error handling (wrap in try/except + log, matching the other
background loops).

### Skipped: lazy pool warming
Moving the TLS/warm cost onto the first misses trades a boot spike for
per-request latency spikes — worse for a latency-sensitive TTS path.

### How to confirm the spike is startup (not traffic)
```bash
kubectl -n beta logs deployment/dragontts --since=5m | grep -iE "socket ready|warm|reconcile|ready —"
```
A burst of pool-warm/reconcile lines right after `DragonTTS ready` = startup.
If CPU stays elevated after the spike settles, that's steady-state (note:
stitch is off, so each MISS now does a full-phrase synth — higher per-miss CPU).
