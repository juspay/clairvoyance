# 09 — `server_stt` Design Guide

*Date: 2026-10-27 · Status: design for the production streaming STT server · Based on: the Nemotron load tests in
`loadtest/` (v1 single-process server, v2 split server, front-end sweep, staged runs to 500–600 sockets)*

> Kept here unchanged as the **measured reference** for [main.md](../main.md). The plan
> and step files are built from it; when they disagree on a measured fact, this file wins.

This is the blueprint for writing `server_stt`. Every rule below comes from something we measured, broke or fixed
while load-testing the fine-tuned Nemotron 3.5 streaming model (`mix2_full`) on one RTX 5090. Section 8 lists the
mistakes so nobody repeats them.

---

## 1. The one rule

> **The thread that feeds the GPU must do nothing but feed the GPU.**

The model is not the bottleneck. One batched streaming step for 200 streams takes **90 ms** on the 5090
(0.45 ms per stream). Our first server took **2.3 s** for the same step because the feeding thread was fighting
socket handling and VAD for Python's GIL. Everything in this guide exists to protect that one thread.

---

## 2. Architecture

```
          callers / telephony gateways  (WebSocket, PCM16, 20–100 ms frames)
                               │
            port 8098  ── SO_REUSEPORT (one box)  /  nginx least-conn (many GPUs)
          ┌──────────────┬─────┴────────┬──────────────┐
   ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐
   │ front-end 1│ │ front-end 2│ │ front-end 3│ │ front-end N│   N = 4..8, no model, own GIL
   │ sockets    │ │ sockets    │ │ sockets    │ │ sockets    │   accept WS, VAD, relay JSON
   │ VAD        │ │ VAD        │ │ VAD        │ │ VAD        │
   └──────┬─────┘ └──────┬─────┘ └──────┬─────┘ └──────┬─────┘
          │  ONE batched message per front-end every 10 ms (audio + VAD events of all its sockets)
          └──────────────┴──────┬───────┴──────────────┘
                  ┌─────────────▼──────────────────────────────┐
                  │ MODEL PROCESS  — exactly one per GPU        │
                  │  ingest threads → per-stream state          │
                  │  GPU feeder thread (only torch caller)      │
                  │    continuous batching, CUDA graphs         │
                  │  model ×1 on GPU (Nemotron 0.6B, 11–16 GB)  │
                  └─────────────────────────────────────────────┘
```

| Component | Count | Owns | Must never |
|---|---|---|---|
| Front-end process | 4–8 per GPU (formula in §6) | WebSockets, framing, VAD, JSON replies | load the model, call CUDA |
| Model process | **exactly 1 per GPU** | model weights, encoder caches, hypotheses, GPU loop | handle sockets, run per-frame Python |
| GPU feeder thread | 1 inside the model process | every torch call | wait on I/O, loop over streams in Python |
| Ingest threads | 1 per front-end | unpack batched messages, append audio, open/close turns | do model work |

**Why not `uvicorn --workers N`?** Every worker imports the app, so every worker loads its own model copy (~7 GB
each), runs its own GPU loop, and batches only 1/N of the sockets. Socket work and model work still share a GIL
inside each worker. `--workers` is the right tool only for the front-ends, which hold no model.

---

## 3. Module layout (suggested)

```
server_stt/
  config.py            env/CLI settings (table in §9)
  protocol.py          Deepgram-compatible message schemas (in + out)
  frontend/
    app.py             FastAPI WS endpoints: /v1/listen (Deepgram), /v1/audio/stream (native)
    session.py         per-connection state: VAD, buffers, outbound queue
    vad.py             Silero wrapper (CPU per-connection) — or a thin client of GPU VAD
    ipc_client.py      batches outbound audio/events every 10 ms; receives results
  model/
    main.py            starts model process, spawns front-ends (spawn context, never fork after CUDA)
    ingest.py          one thread per front-end queue
    streams.py         Stream / Utterance state, slot allocation, ring buffers
    scheduler.py       GPU feeder loop: collect → group → step → emit
    engine.py          model load, warm-up, CUDA graphs, batched step
    vad_gpu.py         (phase 2) batched Silero on GPU
  metrics.py           Prometheus exporters (§7)
  tests/
    load_client.py     staged real-audio load generator (port of loadtest/load_client.py)
    score_run.py       WER-under-load scorer
```

---

## 4. The GPU loop (model process)

### 4.1 Loop shape

```python
while True:
    items = collect_ready()            # every utterance with a full 560 ms chunk (+4-frame margin) or a pending final
    if not items:
        wake.wait(0.005); continue
    for (first, last), group in group_by_step_kind(items):   # ≤4 groups: first/later × final/not
        run_group(group, first, last)  # ONE batched model call per group
    emit_results_batched()             # one message per front-end per tick
```

### 4.2 Rules

1. **Continuous batching.** Don't run on a timer and don't wait to fill a batch: take whatever is ready. At low load
   batches are small (fast); under load they grow by themselves (2 → 13 → 60 → 111 in our runs).
2. **Group by step kind.** First-chunk steps (49 frames, no pre-encode cache, `drop_extra_pre_encoded=0`) and final
   steps (`keep_all_outputs=True`) can't share a call with normal steps.
3. **Build each batch with whole-tensor ops.** For every non-first step the chunk starts at the same offset inside
   its feature window, so `x = feats[:, :, rel - pcs : rel + cs]` is one slice. A Python `for` over streams inside
   `run_group` is a red flag (v1's mistake #2).
4. **Encoder caches in preallocated slot tensors** `cache_last_channel[L, MAX_STREAMS, 56, 1024]`,
   `cache_last_time[L, MAX_STREAMS, 1024, 8]`. Gather the batch with `index_select`, scatter back with
   `index_copy_`. No per-stream `torch.cat` / `.clone()` per tick (v2 still does this; 3.6 ms at batch 200).
5. **CUDA graphs for the encoder step**, one per batch-size bucket (8, 16, 32, 64, 128, 256, 512), padding to the
   bucket. This replaces ~1,600 kernel launches per step with one replay and is the biggest remaining win.
6. **RNNT decoding:** `greedy_batch` with `loop_labels=True` and `use_cuda_graph_decoder=True` (already the NeMo
   default for this model; decode is only 7.6 ms at batch 200).
7. **No host syncs inside the loop:** no `.item()`, `.cpu()`, `print(tensor)`. Use pinned host buffers and
   `non_blocking=True` copies. One `torch.cuda.synchronize()` per tick at most.
8. **Detokenize only changed hypotheses.**
9. **Warm up every bucket at startup** (first and later steps), so the first real caller never pays compile cost.
10. **Precision:** fp32 today. bf16 encoder only after held-out WER is unchanged.
11. Start the model process first, then spawn front-ends with the **`spawn`** multiprocessing context. Never
    `fork` after CUDA is initialised.

### 4.3 Feature extraction

- Preprocessor `normalize: NA`, so features can be computed on a short window instead of the whole utterance:
  window = `[idx − pre_cache − 4, idx + chunk + 4)` frames, discarding 4 margin frames at each edge (STFT padding
  and pre-emphasis edge effects). Batched across streams in one preprocessor call.
- Dither is off in eval mode; outputs are deterministic. Same audio gives the same transcript at any load
  (measured: 100% identical transcripts at 50 sockets vs 8).

---

## 5. Front-ends, VAD and protocol

### 5.1 Front-end rules

1. Forward every audio frame to the model process **immediately** (batched every 10 ms). Run VAD on its own
   cadence (64 ms blocks) and send VAD decisions as separate events. Holding audio until VAD ran cost +50 ms of
   partial lag.
2. One IPC message per front-end per 10 ms, **never one per frame**. 400 sockets × 50 frames/s = 20k messages/s
   would recreate the GIL problem inside the model process.
3. `orjson` for JSON, `uvloop` for the event loop.

### 5.2 VAD

1. **Never share a Silero instance** between connections or threads; the TorchScript module keeps its hidden state
   on itself and crashes under concurrency.
2. Phase 1 (today): one Silero instance per connection on the front-end, in a small thread pool. Cost: ~1.2% of a
   core per talking socket, about 80% of all front-end CPU.
3. Phase 2 (recommended): **batched Silero on the GPU inside the model process**, explicit state tensor
   `[2, MAX_STREAMS, 128]`, one call for all streams every 32 ms. Front-ends then only move bytes.
4. Endpointing settings per tenant: silence 500 ms, threshold 0.5, pre-roll 300 ms, max turn 30 s. The silence wait
   is ~0.5 s of the ~0.6–0.8 s final lag, so it is the biggest lever on perceived latency.

### 5.3 Protocol

- Keep **Deepgram-compatible** `/v1/listen` (pipecat / clairvoyance plug in unchanged) plus the native
  `/v1/audio/stream`.
- Accept 8–48 kHz PCM16, 20–100 ms frames; **recommend 100 ms** frames to clients (5× fewer messages).
- Every partial carries `audio_end_s` (stream position it covers); every final carries `speech_end_s`, `detect_s`,
  `server_ms`. Without these, lag cannot be measured in production.
- **Admission control:** cap open streams per GPU (start at the planned concurrency in §6) and reject extra ones with
  close code **1013** so the load balancer retries elsewhere. One extra caller must never slow everyone down.
- WebSocket ping 20 s / timeout 60 s. On shutdown, stop accepting, finish open utterances, then exit.

---

## 6. Capacity numbers to design for (RTX 5090, `mix2_full`, fp32)

| Item | Value | Source |
|---|---|---|
| Planned concurrency per GPU | **350–400 continuously talking streams** | staged runs, partial-lag p95 ≤ 0.35 s |
| Burst ceiling | 500 streams (0 failures, p95 0.49 s, spikes to 2–3 s) | staged run |
| Equivalent voice-agent calls | ≈ 780–890 per GPU at a 45% caller talk ratio (estimate) | talkers ÷ talk ratio |
| GPU theoretical ceiling | ≈ 1,400 streams (560 ms ÷ 0.4 ms per stream) | standalone step profile |
| VRAM | 11 GB idle → 16 GB at 500 streams | nvidia-smi |
| Front-end CPU | ≈ 1.6% of a core per talking socket | /proc sampling, 400 sockets |
| Front-ends | `N = min(8, max(4, ceil(S_peak / 100)))` | front-end sweep |
| Model process CPU | 1 core, pinned at ~105% | the current limit |

Front-end sweep at 400 sockets (one model process each time):

| Front-ends | Partial lag p95 / p99 | GPU util |
|---|---|---|
| 1 | 7.9 / 10.4 s | 63% |
| 2 | 0.50 / 3.8 s | 62% |
| 4 | 0.32 / 0.55 s | 60% |
| 8 | 0.32 / 0.56 s | 56% |
| 24 | 0.39 / 0.62 s | 51% |

More front-ends never raise GPU utilization; past 8 they add IPC load on the model process's GIL. To use more of the
GPU, make the feeder thread cheaper (§4.2 rules 4 and 5), not add processes.

---

## 7. Metrics to export (Prometheus)

| Metric | Why it matters |
|---|---|
| `stt_batch_size` (histogram), `stt_step_ms` (p50/p95) | GPU-bound vs overhead-bound |
| `stt_feeder_busy_ratio` | The real saturation signal. GPU util alone misleads (it *fell* as load rose in v1) |
| `stt_partial_lag_seconds`, `stt_final_server_seconds` (p50/p95/p99) | User-facing SLA |
| `stt_streams_open`, `stt_streams_speaking`, `stt_rejected_total{code="1013"}` | Capacity and admission |
| `stt_frontend_cpu_ratio{fe=…}`, GPU util, VRAM | Sizing and alerts |
| `stt_finals_total`, `stt_empty_finals_total` | Quality drift under load |

Alert when partial-lag p95 > 0.5 s for 1 min, feeder busy > 95% for 5 min, or any 1013 rejections.

---

## 8. Mistakes we made (don't repeat them)

| # | Mistake | What we saw | Fix |
|---|---|---|---|
| 1 | Sockets, VAD and GPU loop in one Python process | Broke at 200 sockets: p99 3.2 s, spikes 6 s; GPU 72% idle; util fell as load rose | Split: model process + front-ends (§2) |
| 2 | Per-stream Python loops inside the batched step | Step time grew with batch while the GPU waited | Whole-tensor batch building (§4.2.3) |
| 3 | Reading `nvidia-smi` util as "spare capacity" | "40% used" while the feeder was saturated | Watch feeder busy %, batch size, step time (§7) |
| 4 | Planning `--workers N` to scale | Would have loaded the model N times and shrunk batches | One model process per GPU |
| 5 | Guessing the front-end count | 1–2 choke (p95 8 s); >8 slightly worse | Measured formula (§6) |
| 6 | Measuring lag without the 4-frame margin | Smoke test showed a flattering 10 ms | Measure from end of chunk audio |
| 7 | Front-end held audio until VAD ran | +50 ms partial lag | Forward immediately, VAD as events (§5.1) |
| 8 | Shared Silero instance | TorchScript crashes under concurrency | One instance per stream, or batched with explicit state |
| 9 | Keeping the whole stream's audio | Fine for 3-minute tests, a leak on 30-minute calls | Ring buffer, drop consumed audio |
| 10 | FastAPI handler types imported inside a function with `from __future__ import annotations` | WebSocket route returned HTTP 403 | Import `WebSocket` at module level |
| 11 | Scoring Kannada against Kannada-script references | WER 113% | Nemotron 3.5 writes Kannada speech in Devanagari (no Kannada support): route Kannada to a model that supports it, or transliterate before scoring |

---

## 9. Configuration

| Setting | Default | Notes |
|---|---|---|
| `MODEL_PATH` | `finetune/experiments/nemotron/mix2_full/best.nemo` | one per process |
| `LANG_PROMPT` | `auto` | `hi-IN` to force Hindi |
| `RIGHT_CONTEXT` | `6` (560 ms lookahead) | 0/1/3/6/13 trade latency for accuracy |
| `PORT` | `8098` | front-ends share it |
| `FRONTENDS` | `0` = auto (`min(8, max(4, ceil(PLAN_SOCKETS/100)))`) | |
| `PLAN_SOCKETS` | `400` | also the admission cap |
| `VAD_MIN_SILENCE_MS` / `PREROLL_MS` / `MAX_UTT_S` | `500` / `300` / `30` | per-tenant override |
| `FLUSH_MS` | `10` | front-end → model batching interval |
| `MAX_BATCH` | `512` | largest CUDA-graph bucket |

---

## 10. Testing checklist before launch

1. Staged load test with **real recorded caller audio at real-time pace** from ≥ 6 client processes; client send
   slip p99 must stay < 10 ms or the test is measuring the client.
2. Each stage 2–3 minutes; partial-lag p95 in the last 30 s must not exceed the first 30 s (no backlog growth).
3. **WER per stage** against the same references (`score_run.py`): accuracy under load must match low load.
4. Front-end sweep at the planned concurrency.
5. One-hour soak at the planned concurrency; VRAM and RSS flat.
6. Over-cap test: planned + 25% sockets → the extra ones get 1013, accepted ones keep their SLA.

---

## 11. Build order

1. Port v2 (`loadtest/nemotron_server2.py`) into the module layout above, with the Deepgram protocol.
2. Admission control + Prometheus metrics.
3. Slot-tensor caches, then CUDA graphs per bucket → rerun the staged test to 800+ sockets.
4. Batched GPU VAD → rerun the front-end sweep.
5. Ring buffers + soak test.
6. Language routing for Kannada (Nemotron 3.5 has no Kannada support).
