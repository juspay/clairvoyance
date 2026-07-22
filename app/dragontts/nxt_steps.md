# DragonTTS — Next Steps & Improvement Roadmap

> Goal: make DragonTTS the **fastest, most reliable, highest-quality** TTS caching
> proxy it can be. This doc captures every worthwhile improvement found across
> speed, reliability, quality, scalability, observability, cost, security, ops,
> and testing — grounded in the current architecture and in current (2026)
> best practice. No code here; this is the plan.

---

## 0. Where we are today (baseline)

- **Stack:** FastAPI (uvloop) + httpx + websockets + numpy + pydantic v2; SQLite (WAL,
  per-thread conns) + filesystem blobs on a single RWO PVC.
- **Providers (all native `pcm_s16le@16k`, convert-on-serve):** Cartesia (warm
  multi-context WS pool), ElevenLabs **Indian residency only** (warm multi-context WS
  pool), Sarvam (warm LIFO WS pool), Gemini (cached gRPC `streaming_synthesize`).
- **Cache:** format-agnostic key (`sha256(text+provider+voice+model+language+params)`,
  output_format excluded); native 16k stored once, converted to requested format on
  serve (μ-law 8k for telephony). Single-flight on `/tts/bytes`; predictive warming +
  stitching; streaming resampler; seam smoothing; circuit breakers + one-shot fallback.
- **Verified live:** Cartesia / ElevenLabs-residency / Sarvam synth+stream+cache all
  green; TTFB ~120–260ms; 92 unit tests + hermetic mock-server e2e + real-server DB
  persistence proof all pass.

**Guiding principles** for everything below: (1) never regress TTFB on a hit
(sub-ms) or a miss (provider-bound); (2) the cache is the product — hit rate and
correctness outrank cleverness; (3) degrade gracefully — a provider hiccup must never
become a user-facing hang or a double-bill; (4) keep it operable by one person
(config-driven, observable, no exotic deps until justified).

---

## 1. Latency & Speed ⚡

The only number that matters to a caller is **time-to-first-audio (TTFA)**. Industry
threshold: **>300ms starts to feel like dead air**; 2026 leaders are Cartesia Sonic-3.5
(~40ms TTFB), ElevenLabs Flash v2.5 (~75ms inference). Our warm pools already remove
the WS handshake from the hot path; the remaining levers:

- **Add single-flight to `/tts/stream`** (currently only `/tts/bytes`). N concurrent
  identical streaming misses each open their own provider stream today — that's N×
  synth, N× billing, N× metrics. Coalesce onto one in-flight producer and tee the
  chunks to the waiters. (Documented deferral — this is the highest-value latency/cost
  item.)
- **ElevenLabs latency knobs — use the right one per path.** The **multi-stream-input
  WebSocket** (what we use) takes **`auto_mode`** — already `true` (that's the WS latency
  knob). `optimize_streaming_latency` (0–4) is an **HTTP / older stream-input** param; set
  it on the one-shot/fallback path if that matters, but it's **not** a WS connect-URL param.
- **Speculative prefetch from the LLM token stream.** Today warming is frequency-based
  (recurring substrings). The bigger win: as clairvoyance streams LLM tokens, speculate
  the likely next 1–2 sentences and pre-synth them so they're warm before the TTS
  request arrives. This collapses perceived latency to near-zero on the conversation
  path.
- **Tune `elevenlabs_stream_idle_timeout`** (now configurable, default 0.8s) — the
  trailing close window. Lower = faster turn-end detection; watch for truncation on
  long utterances and raise if needed.
- **(Resolved: no app-level delivery cache.)** An earlier idea here — cache a converted
  μ-law variant for hot entries — conflicts with both the §5 "store native / one canonical
  codec, convert on serve" direction **and** the decision to drop the in-memory blob LRU
  (rely on the OS page cache). Keep convert-on-serve; the format lever is the storage
  codec (§5), not an in-process delivery cache.
- **Write-behind the per-HIT DB write (real, measure-first).** Today every cache HIT
  synchronously `await`s `touch_and_record` (a SQLite WAL commit) on the read path
  *before* returning audio — so HIT TTFA includes a serialized writer. A single small
  UPDATE is ~sub-ms, so the real cost is **serialized-writer contention at target
  concurrency** — measure that at 50–80 parallel before committing to the magnitude.
  Moving hit/metric updates to a batched write-behind queue (many hits → one commit) lets
  the HIT return audio immediately. *(See §14.1.)*
- **Serve HIT blobs with less copying.** `mmap` the blob file read-only so the read comes
  straight from the OS page cache (no full `bytes` allocation). Note `os.sendfile` /
  `FileResponse` only apply when serving the stored file **verbatim** (native-format hit);
  the common convert-on-serve path returns **computed** bytes (μ-law/Opus) which can't be
  sendfile'd — so this is a narrow win, not a general one.
- **HTTP cache headers for HITs.** Add a stable `ETag` + `Cache-Control: immutable` and
  honor `If-None-Match` → `304`. **Caveat: the ETag must be `hash(key + output_format)`,
  NOT the cache key alone** — the key deliberately excludes `output_format` (one key
  serves μ-law and PCM bytes), so a key-only ETag would `304` a wrong-format response.
  Cheap; lets clairvoyance / a CDN skip re-downloading identical clips with correct
  revalidation.
- **Transport:** ensure `TCP_NODELAY` (no Nagle) on provider sockets and the server;
  confirm uvloop is on; consider **Granian** or HTTP/2 (Hypercorn/h2) for the
  dragonTTS↔clairvoyance link if chunked HTTP/1.1 streaming shows head-of-line stalls.
- **First-chunk priming** is already in `/tts/stream` (good) — extend the same
  "return the first byte fast, then stream" discipline everywhere.
- **Resampler:** linear-interp numpy is fine for speech; if quality complaints arise,
  swap in **soxr** (band-limited) for the resample, vectorized.
- **Measure TTFA as a first-class metric** (see §7) — you can't optimize what you don't
  time per stage.

---

## 2. Cache Effectiveness & Hit Rate 🎯

Hit rate is the single biggest cost/latency lever. Every 1% of misses avoided is
provider calls + billing + latency saved.

- **Deterministic text normalization before keying — but only audio-equivalent
  transforms.** Today: emoji-strip (in clairvoyance) + canonical params. Safe to add:
  trim, collapse internal whitespace, Unicode NFKC, strip surrounding quotes. **Danger:**
  case-folding (`"US"`→`"us"`) and number/abbreviation expansion (`"10%"`→`"ten percent"`,
  `"Dr."`→`"Doctor"`) can change **pronunciation/emphasis** — normalizing the *key* only
  means same-key/different-text → **wrong audio served**; normalizing the *text sent*
  changes the audio. Only fold forms **guaranteed** to synthesize identically; otherwise
  leave them. Biggest hit-rate win for the least risk *if you stay conservative*.
- **Number / abbreviation normalization** (e.g. "10%" vs "ten percent") — same caveat:
  only collapse forms whose spoken output is provably identical (or pin the *spoken* form
  on input). High value, but delicate.
- **Bloom filter for fast negative lookup.** A tiny in-memory Bloom of cached keys →
  skip the SQLite probe on obvious misses. **Caveat:** a plain Bloom can't track
  evictions — churn accumulates stale "maybe-present" positives that erode the skip-probe
  benefit. Use a **counting Bloom** or periodic rebuild if eviction is active.
- **LFU / W-TinyLFU eviction.** Current eviction is size/LRU-based. Voice traffic is
  heavily skewed (a few phrases dominate). **W-TinyLFU** (frequency + recency) keeps
  the hot scripted lines while still aging out one-offs — higher steady-state hit rate
  than plain LRU at the same byte budget.
- **TTL / freshness.** Add optional TTL for entries that may go stale (voice/model
  upgrades) without a full flush. Pin a cache **schema version** in the key prefix so a
  normalization/codec change cleanly orphans old entries (already a known gotcha —
  formalize it).
- **Negative caching.** Remember recently-failed phrases (provider error / empty audio)
  for a short TTL so a flaky phrase doesn't hammer the provider.
- **Stitching improvements.** Raise `predictive_stitch_min_coverage` tuning, improve
  crossfade at seams, handle the edge cases (single-word gaps, boundary alignment).
  Stitching turns partial-cache coverage into a hit — high value for conversational
  traffic.
- **Cross-utterance warming.** When synthesizing phrase P, speculatively warm the
  phrases that historically follow P (bigram-of-phrases from the frequency tracker).

---

## 3. Reliability & Resilience 🛡️

A TTS proxy sits on the critical path of a voice call; every failure is dead air or a
dropped call. Resilience is non-negotiable.

**Close the open code-review items (P0 correctness — confirmed still present in §14.2):**
- **Mid-stream socket close → 502 (`elevenlabs_pool` `_mark_all_dead` on a *clean*
  idle-close).** Distinguish a benign reconnect from a hard failure: not-yet-streamed
  contexts should retry/reacquire or fall back to one-shot synth, not surface 502.
- **Post-send WS fallback (residual double-bill window).** Mid-stream double-billing is
  **already blocked** — both ElevenLabs and Sarvam providers refuse to fall back once any
  audio has streamed (`if streamed_any: raise`). The narrow residual: `init+text+flush`
  were sent but no audio arrived yet, then the WS errors → one-shot HTTP fallback
  re-synthesizes (and re-bills) the same utterance — which is already the chosen
  resilience policy (serve the caller over saving one synth). Decide only whether the
  billing risk warrants refusing fallback after the utterance is committed to the WS.
  *(Low priority — most paths already guarded.)*
- **Sarvam mid-stream `ConnectionClosed`.** **Behaviorally already handled** — it
  propagates as a generic `Exception` into the provider fallback guard (falls back when
  nothing streamed, raises when audio already flowed). Only nicety left: wrap it as a
  typed `ProviderError` (like `TimeoutError`) for cleaner metrics/logs. *(Low priority.)*
- **Bound the `_pools` dicts** (`elevenlabs` keyed by (voice,model), `sarvam` by model)
  — currently unbounded; a stray voice/model leaks a pool of sockets + tasks forever.
  Add an LRU cap with `aclose` on eviction.
- **Sarvam busy-wait** (`sleep(0.1)` while `_busy`) → replace with an `asyncio.Event`
  signaled on release (zero wakeups during an utterance).

**New resilience work:**
- **Per-provider token-bucket rate limiting** (`aiolimiter`). Stay under provider
  concurrency/character limits to avoid 429s and billing spikes; essential as traffic
  grows.
- **Bulkheads.** Cap concurrent in-flight synths **per provider** (asyncio.Semaphore)
  so one slow/hung provider can't starve the worker pool and stall the others.
- **Retry with backoff + jitter** (`tenacity`) for transient provider 5xx/network
  errors — but **gate retries behind the circuit breaker** to prevent retry storms
  (Marc Brooker's classic: breaker to stop retries, token bucket to allow first-tries).
- **Provider failover / cascade.** If Cartesia is unhealthy, synthesize the same
  language on ElevenLabs/Sarvam (with a voice-parity map). This is the **on-error** arm
  of one provider-selection strategy — pair with **on-slow** (request hedging, §14.1) and
  **on-cost** (cost-aware routing, §6).
- **Idempotent, atomic cache writes** — `put_with_totals` is now transactional (done);
  extend the same atomic discipline to any future multi-statement write.
- **Never hang.** Audit every `await` on a provider path for a bounded timeout
  (connect, first-audio, recv, full). A missing timeout = a potential indefinite hang
  under provider degradation.
- **Crash/fsync safety.** **Confirmed open:** blob writes use plain `write_bytes` with
  no `fsync` — a crash/SIGKILL can lose the newest blobs and the metadata row vs blob
  can disagree. fsync blob writes (or a durable-write helper); keep the WAL checkpoint
  policy; survive SIGKILL without cache corruption.
- **Graceful shutdown.** Wire `registry.aclose_all()` into the lifespan shutdown so
  pools/providers (esp. the cached Gemini gRPC channel) are closed cleanly; drain
  in-flight requests before exit.

---

## 4. Audio Quality 🎧

- **Opus for delivery (not just μ-law).** Opus gives dramatically better quality at a
  fraction of the bitrate (speech-transparent ~24–32 kbps vs μ-law's 64 kbps) with
  ~26.5ms frame latency — ideal for streaming/VoIP. Offer Opus alongside μ-law; let
  clairvoyance pick per channel. (μ-law stays for legacy Twilio.)
- **Loudness normalization across providers (LUFS).** Switching Cartesia↔ElevenLabs↔
  Sarvam today can jump volume. Normalize to a target LUFS on store or serve so a
  fallback/failover is inaudible.
- **Cross-utterance prosody continuity.** ElevenLabs supports `previous_text` (and the
  with-timestamps endpoint), Cartesia has **Continuations** — use them so concatenated
  cached clips don't sound spliced. Today seam smoothing is numpy DC/trim/RMS/equal-
  power; escalate to provider-native continuity when quality demands.
- **Resampler quality** — linear is fine for speech; offer band-limited (soxr) for
  music/high-fidelity voices.
- **Validate every synth** (non-empty, peak > threshold, even length) in production,
  not just smoke — refuse to cache silence/garbage and fall back instead.
- **DC-offset / clipping detection** on ingest; auto-reject bad clips.

---

## 5. Scalability & Storage 📈

Today: single pod, SQLite + filesystem on RWO PVC. This is correct up to one pod's
throughput. Plan the multi-pod path **before** you hit it.

- **The multi-pod trigger is the storage.** When you need >1 pod (HA / throughput),
  the PVC can't be shared RWO. Options, best-fit first:
  - **LibSQL / Turso (replicated SQLite)** — keeps the SQL + simple ops, adds primary
    ↔ replica replication so every pod reads local (sub-ms) and writes forward. The
    cleanest evolution of the current design; the SQL barely changes.
  - **LiteFS** — Fly.io-style distributed SQLite (primary + read replicas on local
    disk), good if self-hosting.
  - **Postgres** only if you outgrow SQLite-class workloads (it's a bigger ops lift;
    avoid unless justified).
  - Blobs move to **object storage (S3/GCS)** with the DB still local, OR a shared
    blob store. Prefer keeping hot blobs local (PVC) and tiering cold ones to object.
- **Storage compression — biggest disk win, but it trades zero-copy serve.** Raw
  `pcm_s16le@16k` is ~32 kB/s. Storing **Opus** (~24 kbps) cuts blobs ~90–95%; **FLAC**
  ~50–60% **losslessly**. Decode on serve (adds decode CPU to every HIT). **Tension:** a
  compressed blob **can't be `sendfile`'d** — it must be decoded first, so this competes
  with the §1 mmap/zero-copy idea (pick per path). Prefer FLAC (lossless, exact
  round-trip) as the canonical, derive delivery formats on serve — never lose quality to a
  codec change.
- **Content-addressed blob dedup (marginal).** Hash the *bytes* and refcount by content
  hash → store identical clips once. **Low value unless normalization is collapsing
  keys** — different text rarely yields identical bytes, so dedup hits are uncommon.
- **Tiered storage / CDN at scale.** For read-heavy global delivery, put hot blobs
  behind a CDN with long cache headers keyed by the (immutable, content-derived) key —
  edge serves repeat hits without touching dragonTTS at all.
- **Read replicas** (Turso/LiteFS) scale the metadata reads (the probe on every
  request) horizontally.
- **Eviction by bytes** (cap total cache size) — already supported (`max_cache_bytes`);
  wire alerting on it.

---

## 6. Provider Strategy & Cost 💰

- **Cost-aware routing.** Pick provider per language/voice by latency SLA **and** cost:
  Sarvam (cheap, Indian languages), Cartesia (fastest), ElevenLabs (quality). A routing
  policy can default to the cheapest that meets a latency SLO.
- **Failover cascade** doubles as cost control — primary fast/expensive, fallback
  cheap/resilient.
- **Kill double-billing** (#3 above) and enforce single-flight everywhere — every
  redundant synth is literal money.
- **Character-budget tracking.** Per-provider spend counters + alerts; auto-throttle
  near quota.
- **Voice-parity map across providers** so a "persona" resolves to the right voice on
  whichever provider serves it (important once failover is on).
- **Residency as a first-class concept** — keep the Indian-residency path (latency +
  data sovereignty for India); consider other regions as traffic warrants.

---

## 7. Observability 🔭

You can't hit an SLO you don't measure. Voice wants **P50/P90/P99** (P99 captures the
callers who hang up), not just averages.

- **OpenTelemetry tracing** end-to-end: clairvoyance → dragonTTS → provider, with
  spans per stage (cache lookup, acquire, synth, convert, store). One trace shows
  exactly where the milliseconds go.
- **Metrics (Prometheus-style):**
  - Latency: TTFA p50/p90/p99, total-stream p99, cache-lookup p99, convert p99.
  - Cache: hit/miss/stitch rates, eviction count/bytes, entry count, byte total.
  - Providers: per-provider synth Calls, errors (by code), TTFB, in-flight, pool
    sockets/contexts, circuit state, rate-limit near-quota.
  - RED: Rate, Errors, Duration per endpoint.
- **SLOs:** TTFA p99 **< 20–50ms on HITS** (local: blob read + convert + touch) and
  **< 300ms on MISSES** (the provider-bound dead-air threshold); hit rate > 90%
  steady-state; provider error rate < 1%; alert on burn rate.
- **Dashboards (Grafana)** — cache health, per-provider latency/cost/error, pool
  saturation.
- **Structured logs with request/trace IDs** (loguru is in place) — never log full text
  or keys/secrets in prod (hash/prefix only — a real Cartesia key leaked in chat
  earlier; harden this).
- **Periodic audio quality audit** — sample served clips, score (MOS where feasible),
  catch silent/garbage regressions.

---

## 8. Security 🔒

- **Secret hygiene.** Keys via k8s Secret (already); rotate (esp. the previously-leaked
  Cartesia key); guarantee no key/secret ever appears in logs, error bodies, or
  responses.
- **Input validation & allowlists.** Cap text length; allowlist voice/model/
  output_format; reject absurd params — prevents abuse and cost runaway.
- **Client rate limiting** (per API key / IP) — protect against floods and accidental
  loops that would mint provider spend.
- **Auth on dragonTTS endpoints** if ever exposed beyond the trusted internal network
  (API key / mTLS).
- **PII / retention.** Cached `text` may carry PII; set a retention/flush policy; avoid
  logging transcripts in prod; consider keying-only (don't store raw text longer than
  needed — today `text` is a column; weigh storing just the hash).
- **SSRF** — `base_url` is server-side config (low risk); keep it that way (never let a
  request influence provider URLs).

---

## 9. Operational Excellence 🚀

- **Readiness vs liveness.** `/health` (process up) + `/ready` (providers warmed AND
  at least one reachable). K8s readiness should reflect real serving ability.
- **Graceful shutdown** — drain in-flight, `aclose_all` pools/providers, exit clean
  (ties to §3).
- **Config validation + a settings reference doc** (every env var, default, effect).
- **Deployment artifacts** (Dockerfile, k8s manifest with probes/resource limits/PVC) —
  user runs infra, so provide reviewed, documented artifacts.
- **CI:** run the 92 unit tests + the hermetic mock-server e2e on every PR; gate merges
  on green.
- **Backups:** PVC snapshot cadence; DB dump; verify restore.
- **Cache key schema versioning** (§2) for zero-downtime upgrades.
- **Admin API:** extend `/stats`; add `/cache` inspect/list/delete, `/warm` (trigger),
  `/providers` (live status), `/drain` — operate without k8s exec.

---

## 10. Testing & QA 🧪

- **Already strong:** 92 unit tests, hermetic mock-ElevenLabs e2e, real-server DB
  persistence proof.
- **Load testing (k6 / locust)** at the stated **50–80 parallel** target — measure TTFA
  p99, throughput, pool behavior, DB contention, and the point where single-flight on
  `/tts/bytes` and the per-thread SQLite conns start to bind. This is the test that
  proves the "handle 50–80 parallel with best latency" goal.
- **Chaos:** kill a provider WS mid-stream; inject network partition; fill the disk;
  kill the process mid-write — assert graceful degradation and no corruption.
- **Fuzzing:** empty/very-long/emoji/RTL/emoji-laden/malformed text; bad model_id;
  huge params; concurrent identical + concurrent distinct storms.
- **Property tests** for the resampler (chunked == whole across rates/sizes — already
  partially covered; expand the matrix).
- **Provider contract tests** pinning the WS wire schemas (the Sarvam/ElevenLabs frames
  we reverse-engineered via pipecat + raw probes) so a provider schema change is caught
  in CI, not in prod.
- **clairvoyance ↔ dragonTTS integration tests** (the real consumer).

---

## 11. Developer Experience & API 🧑‍💻

- **Debug headers:** add `X-TTFB-ms`, `X-Provider`, `X-Cache-Lookup-ms`, `X-Synth-Ms`
  to responses so callers (and dashboards) see where time went.
- **OpenAPI + a usage/provider-matrix doc** (FastAPI gives the spec free; add prose).
- **Consistent errors** with machine-readable codes (`payment_required`, `provider_down`,
  `rate_limited`, `bad_model`) so clairvoyance can react.
- **Versioned API** (`/v1/...`) before the interface ossifies.
- **Backpressure signaling** — when saturated, return an explicit retryable status so
  clients shed load rather than pile up.

---

## 12. Prioritized Roadmap

| Pri | Item | Theme | Effort | Impact |
|-----|------|-------|--------|--------|
| **P0** | Single-flight on `/tts/stream` (producer/tee fan-out) | Latency+Cost+Reliability | M | **Huge** — kills N× synth/billing on concurrent streaming misses |
| **P0** | **Write-behind the per-HIT DB write** (queue `touch_and_record`) | Latency+Throughput | M | High — every HIT stops awaiting a WAL commit (measure contention at target load) |
| **P0** | Per-provider rate limiting (`aiolimiter`) + bulkheads | Reliability+Cost | M | High — avoids 429/billing runaway, isolates slow providers |
| **P0** | Text normalization before keying (whitespace/NFKC/punct/numbers) | Hit rate | S–M | **Huge** hit-rate / cost win |
| **P0** | Close remaining code-review items: mid-stream 502 (`_mark_all_dead`), `_pools` cap, Sarvam busy-wait, blob `fsync` | Reliability | M | High — removes confirmed correctness/leak/durability gaps |
| **P1** | Speculative prefetch from LLM token stream (Pipecat "preemptive speech") | Latency | M–L | Huge perceived-latency collapse on the conversation path |
| **P1** | Opus delivery (+ keep μ-law); `opuslib-next`/ffmpeg, ~24–32 kbit/s | Quality+Bandwidth | S–M | Better quality, less bandwidth |
| **P1** | ETag/304 (`hash(key+output_format)`) + `mmap` blob reads | Latency+Cost | S–M | Correct revalidation + cheaper reads (sendfile only for verbatim-native hits) |
| **P1** | `Idempotency-Key` header (dedupe clairvoyance retries) | Reliability+Cost | S | Kills retry-driven double-synth/double-bill |
| **P1** | Coordinated SIGTERM drain before `aclose_all` + `/ready` | Ops | S | No mid-clip truncation on deploy |
| **P1** | Provider failover cascade + voice-parity map | Reliability+Cost | M | Survives a provider outage inaudibly |
| **P1** | Storage compression (FLAC lossless canonical / Opus) | Scalability+Cost | M | ~50–95% disk/object-cost cut |
| **P1** | Load test at 50–80 parallel (k6/locust) | QA | S–M | Proves the headline SLA; finds bottlenecks |
| **P2** | OpenTelemetry tracing + Prometheus + Grafana + SLOs (p50/p90/p99) | Observability | M | Sees the latency budget; alerts before users do |
| **P2** | W-TinyLFU eviction (`theine`) + TTL + schema-version prefix + SWR | Hit rate/Ops | M | Higher steady-state hit rate; safe upgrades; no expiry spike |
| **P2** | Request hedging (Dean & Barroso "Tail at Scale") for p99 | Reliability | M | Caps tail latency on a flaky provider |
| **P2** | Deadline propagation (caller-chosen bound across the chain) | Reliability | M | Bounded turn latency; no over-runs |
| **P2** | Config hot-reload + feature flags (no-restart knob flips) | Ops | M | Flip stitch/pool/TTL mid-incident |
| **P1** | **Remove the in-memory blob LRU** → OS page cache + `mmap` + pod mem limit | Ops+Latency | S | Frees 64MB heap, kills invalidation pain, ~zero latency cost |
| **P2** | Bloom-filter fast negative lookup | Latency | S | Cheaper miss path under load |
| **P2** | LUFS loudness normalization + Cartesia Continuations / ElevenLabs previous_text | Quality | M | Seamless cross-provider / cross-clip audio |
| **P2** | Multi-pod storage: Turso/libSQL embedded replicas + object-store blobs | Scalability | L | Unblocks HA/throughput beyond one pod |
| **P2** | Content-addressed blob dedup + CDN edge caching | Scalability+Cost | M | Dedup + zero-touch repeat hits at scale |
| **P3** | Admin API (inspect/warm/drain/providers), debug headers, versioned API | DX/Ops | M | Operability + caller insight |
| **P3** | Chaos + fuzz + provider contract tests in CI | QA | M | Catches regressions before prod |
| **P3** | PII/retention policy; text-column → hash-only option | Security | S–M | Reduces stored-PII surface |
| **P3** | Probabilistic early expiry (XFetch) for fleet-wide TTL | Cache | S | Lock-free stampede prevention at scale |

**If only four things:** (1) single-flight on `/tts/stream`, (2) **write-behind the
per-HIT DB write**, (3) text normalization, (4) rate limiting + bulkheads. Those four
move latency, cost, hit-rate, and reliability simultaneously.

---

## 13. References (research basis)

Latency & providers:
- [Picovoice — TTS Latency: how to read vendor claims & minimize it](https://picovoice.ai/blog/text-to-speech-latency/)
- [ElevenLabs — Latency optimization guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization)
- [ElevenLabs — Real-time TTS / multi-context WebSocket guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tts)
- [Gradium — TTS Latency Benchmark 2026 (TTFA across providers)](https://gradium.ai/content/tts-latency-benchmark-2026)
- [Cartesia — Sonic-3.5 / Ink-2 launch (sub-90ms TTS)](https://cartesia.ai/launch/)
- [Inworld — Best TTS API 2026 (≤300ms dead-air threshold)](https://inworld.ai/resources/best-tts-api-2026)

Codecs & storage compression:
- [Opus codec — comparison & bitrate/quality (opus-codec.org)](http://opus-codec.org/comparison/)
- [Wowza — Opus codec explained (~26.5ms latency)](https://www.wowza.com/blog/opus-codec-the-audio-format-explained)
- [Audiokinetic — choosing the right codec (Opus vs FLAC vs PCM CPU/size)](https://www.audiokinetic.com/en/community/blog/a-guide-for-choosing-the-right-codec)
- [MDN — Web Audio Codec Guide](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Audio_codecs)

Scalability / distributed SQLite:
- [Turso — Distributed SQLite (official)](https://turso.tech/)
- [Distributed SQLite: why LibSQL/Turso are the 2026 standard](https://dev.to/dataformathub/distributed-sqlite-why-libsql-and-turso-are-the-new-standard-in-2026-58fk)
- [Kent C. Dodds — Postgres cluster → distributed SQLite (LiteFS)](https://kentcdodds.com/blog/i-migrated-from-a-postgres-cluster-to-distributed-sqlite-with-litefs)

Resilience:
- [aiolimiter — async token-bucket rate limiter](https://pypi.org/project/aiolimiter/)
- [Marc Brooker (AWS) — fixing retries with token buckets + circuit breakers](https://brooker.co.za/blog/2022/02/28/retries.html)
- [OneUptime — token-bucket rate limiting in Python](https://oneuptime.com/blog/post/2026-01-22-token-bucket-rate-limiting-python/view)

Observability:
- [Evalgent — Voice agent observability (P50/P90/P99 metrics)](https://www.evalgent.com/blog/voice-agent-observability)
- [FutureAGI — Voice AI observability for real-time production](https://futureagi.substack.com/p/how-to-implement-voice-ai-observability)
- [OneUptime — how to build latency-percentile SLOs](https://oneuptime.com/blog/post/2026-01-30-latency-percentile-slos/view)
- [Python + OpenTelemetry — p95/p99 RED dashboards](https://medium.com/@hjparm1944/python-opentelemetry-metrics-p95-p99-and-red-sla-dashboards-in-minutes-bf4aada92adb)

Voice-agent stack & best practice:
- [Towards AI — Voice AI in 2026: the complete stack](https://pub.towardsai.net/voice-ai-in-2026-the-complete-stack-from-whisper-to-speaker-23e4de3ee3c4)
- [Pipecat — Text-to-Speech (caching/prefetch patterns)](https://docs.pipecat.ai/pipecat/learn/text-to-speech)
- [Rime AI — TTS + voice best practices](https://rime.ai/resources/tts-voice-best-practices)

---

## 14. Post-review addendum (vetted)

Added after fanning out review subagents (gap-hunt, codebase-accuracy check, deep
research). All items below are grounded in the actual code (file:line) and current
(2026) practice.

### 14.1 Highest-leverage NEW finds (not in §1–§13)
- **Write-behind the per-HIT DB write (P0).** Confirmed: every cache HIT runs a
  synchronous `touch_and_record` (SQLite WAL commit) on the read path before returning
  audio. Move hit/metric updates to a batched write-behind queue so a HIT returns audio
  instantly; one commit absorbs many hits → far less fsync cost under load. Biggest
  hot-path win.
- **ETag/304 + mmap reads (P1).** `ETag` = **`hash(key + output_format)`** (NOT the key —
  `output_format` is excluded from the cache key, so a key-only ETag would `304` a
  wrong-format response), `Cache-Control: immutable`, honor `If-None-Match`→304. Read
  blobs via `mmap` (page-cache, no full `bytes` alloc). Note `sendfile`/`FileResponse`
  only work for verbatim-native hits — convert-on-serve returns computed bytes.
- **`Idempotency-Key` header (P1).** Dedupe clairvoyance retries by (cache key +
  idempotency key) for a short window — the standard fix for retry-driven double-synth/
  double-bill that in-process single-flight doesn't cover.
- **Coordinated SIGTERM drain (P1).** Flip `/ready` false, reject new with 503 +
  `Connection: close`, await active `StreamingResponse` generators to zero (or a
  deadline) **before** `aclose_all` — today a pod term can truncate in-flight audio.
- **Request hedging for tail latency (P2).** Dean & Barroso, "The Tail at Scale": fire
  the primary synth; if it exceeds its p95 TTFB, fire a hedge on the fallback provider,
  take whichever audio returns first, cancel the loser. Gate hard: hedge only after the
  delay, never into a tripped breaker, budget hedges in the same token bucket so they
  can't double cost.
- **Deadline propagation (P2).** Accept a per-request deadline and bound synth/recv/
  store timeouts by remaining time so the whole clairvoyance→dragonTTS→provider chain
  respects one caller-chosen bound (how real-time voice keeps turn latency bounded).
- **Config hot-reload + feature flags (P2).** Watch settings and apply pool/stitch/TTL/
  warming knobs at runtime — flip stitch off or raise a pool during an incident with
  zero deploy (matters for a single-pod service).
- **Remove the in-memory blob LRU entirely (decision).** The OS page cache already caches
  hot blob pages for free (kernel-managed, self-invalidating); the app LRU's marginal
  latency win (~tens of μs over a page-cache hit) is negligible next to convert + touch,
  it costs 64MB of heap, and its invalidation is a correctness hazard. Drop it, rely on
  the page cache (+ `mmap` reads), and bound the pod's memory via a k8s limit. (Supersedes
  the earlier "OOM-safe blob LRU" idea — don't make the LRU smarter, delete it.)
- **Stale-while-revalidate + XFetch (P2/P3).** Serve stale on TTL expiry + background
  refresh (no TTFA spike); jitter refresh with probabilistic early expiry
  `P = 1 − exp((Δ−δ)/β)` so a fleet doesn't stampede one hot key at TTL.

### 14.2 Corrections to §3 (accuracy — verified against code)
- **Double-billing:** mid-stream double-billing is **already blocked** — both providers
  refuse to fall back once audio streamed (`if streamed_any: raise`, elevenlabs.py +
  sarvam.py). Only the narrow "init sent, no audio yet, then WS error → one-shot
  fallback" residual exists, and that's the deliberate resilience policy. *(Downgraded
  from "open bug" to "policy decision.")*
- **Sarvam `ConnectionClosed`:** **behaviorally already handled** — propagates as a
  generic `Exception` into the provider fallback guard (falls back if nothing streamed,
  raises if it did). Only nicety left: wrap as a typed `ProviderError` for cleaner
  metrics. *(Downgraded.)*
- **Still genuinely open (confirmed present in code):** mid-stream 502 from
  `_mark_all_dead` on a *clean* idle reconnect; unbounded `_pools` dicts (elevenlabs by
  (voice,model), sarvam by model); Sarvam `sleep(0.1)` busy-wait; and **blob writes are
  not fsync'd** (`path.write_bytes`, no fsync) — a crash can lose the newest blobs and
  disagree with the metadata row.
- **Confirmed DONE (no work remaining):** `put_with_totals` transactional, metrics
  miscount fix (`produced` flag), Gemini gRPC cancel-on-close, registry `warm` gather,
  `elevenlabs_stream_idle_timeout` configurable, single-flight on `/tts/bytes`,
  ElevenLabs residency-only routing.

### 14.3 Concrete tooling / sharpenings (from research)
- **Single-flight on `/tts/stream`** → implement as a one-producer/many-reader **tee**
  (request coalescing; same pattern as nginx `proxy_cache_lock` / FusionCache). For
  multi-pod, move the dedupe to a distributed lock (Redis `SET NX`).
- **W-TinyLFU eviction** → use **`theine`** (PyPI, Rust core, Caffeine-inspired adaptive
  W-TinyLFU) — don't hand-roll. Beats LRU on the skewed/Zipfian distribution voice
  traffic has (Count-Min-Sketch admission blocks one-off phrases from evicting hot lines).
- **Opus cache** → encode on write, decode-on-serve with **`opuslib-next`** (maintained
  fork; needs system libopus) or **ffmpeg/PyAV** for whole-file; target **~24–32 kbit/s
  for speech**. The ~26.5ms Opus frame delay only matters on the live path, not a blob.
- **Multi-pod storage** → **Turso/libSQL embedded replicas** via the **`libsql`** package
  (not the deprecated `libsql-experimental`); each pod reads local SQLite (sub-ms),
  writes forward to a primary. Caveats: write-then-read across pods is eventually
  consistent (not read-your-writes); remote/HTTP mode loses true local transactions —
  keep atomic writes on the local path.
- **Speculative prefetch** → an open technique (Pipecat issue #3321 "preemptive speech
  generation"); proven patterns: start TTS at the first sentence boundary of the LLM
  token stream, inject filler clips while the LLM thinks (Vapi does this), and
  speculatively pre-synth the likely next 1–2 sentences and discard on mismatch.

### 14.4 New references
- [Dean & Barroso — The Tail at Scale (hedged requests)](https://cacm.acm.org/research/the-tail-at-scale/)
- [Optimal Probabilistic Cache Stampede Prevention — XFetch paper](https://cseweb.ucsd.edu/~avattani/papers/cache_stampede.pdf)
- [Cloudflare — Sometimes I Cache (lock-free stampede prevention)](https://blog.cloudflare.com/sometimes-i-cache/)
- [theine — Rust-core W-TinyLFU cache for Python](https://pypi.org/project/theine/)
- [opuslib-next — maintained Opus ctypes bindings](https://pypi.org/project/opuslib-next/)
- [Turso — embedded replicas (Python `libsql` SDK)](https://docs.turso.tech/features/embedded-replicas/introduction)
- [Pipecat #3321 — preemptive speech generation](https://github.com/pipecat-ai/pipecat/issues/3321)
- [Vapi — how we built the voice AI pipeline (stream, not batch)](https://vapi.ai/blog/how-we-built-vapi-s-voice-ai-pipeline-part-1)
- [OneUptime — request coalescing (nginx proxy_cache_lock pattern)](https://oneuptime.com/blog/post/2026-01-25-request-coalescing/view)
