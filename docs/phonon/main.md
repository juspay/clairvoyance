# Phonon — main plan

Self-hosted streaming speech-to-text for Clairvoyance, running our fine-tuned
Nemotron 3.5 model (`mix2_full`) on our own GPUs. It is first used alongside Sarvam
(shadow mode) and then, if it holds up, as the main STT provider.

**Status:** design agreed · design load-tested with prototypes · production server not built yet.
**Start here:** PR-01 is built in three parts — [PR-01.1 Core](prs/pr-01-1-core.md), then [PR-01.2 Model process](prs/pr-01-2-model-process.md), then [PR-01.3 Front-end + supervisor](prs/pr-01-3-frontend-supervisor.md).
**Full design doc (reasoning, diagrams, review):** https://claude.ai/artifact/CiNPw3DLD1Gr2HdpbK7vkP

## This folder

```
docs/phonon/
  main.md                              this file: the whole plan, every piece of work
  prs/                                 one doc per PR, written when we reach it
    pr-01-1-core.md
    pr-01-2-model-process.md
    pr-01-3-frontend-supervisor.md
  reference/
    load-test-design-guide.md          measured rules and numbers (wins on any measured fact)
```

Earlier documents: `nemotron_stt_service_plan.pdf` (design and costs) and
`NEMOTRON_STT_HANDOFF.md` (build brief). Where they disagree with this folder, this folder
is the latest decision. Every PR doc links back here; when a PR changes a decision,
it updates this file in the same commit.

---

## 1. What we are building

| Piece | Where | What it does |
|---|---|---|
| **STT server** | `phonon/` at the repo root: self-contained, own `pyproject.toml`, `uv.lock`, `Dockerfile`, image | Loads the model once per GPU and serves hundreds of concurrent calls over WebSocket, one socket per call |
| **Clairvoyance side** | `STTProvider.NEMOTRON` (`app/ai/voice/agents/breeze_buddy/template/types.py`) + a branch in `create_stt_from_config` (`app/ai/voice/agents/breeze_buddy/stt/__init__.py`) | Its own **`NemotronSTTService`** (`app/ai/voice/stt/nemotron.py`, a Pipecat STT service) speaks Phonon's Deepgram-style protocol. We add that client, provider wiring, fallback, shadow mode |
| **Infrastructure** | GKE (asia-south1, `breeze-automatic-prod`), GPU node pool, GCS model bucket | Runs the server next to Clairvoyance, internal-only |

Telephony, VAD/turn detection, LLM and TTS are unchanged.

### Why a Deepgram-style protocol

Clairvoyance talks to Phonon through its own `NemotronSTTService`, but the
messages follow Deepgram's live format instead of something invented:
- It is a well-known, documented shape for streaming STT (connect params in the
  URL, `Results` with `is_final` / `speech_final` / `from_finalize`,
  `Finalize` / `KeepAlive` / `CloseStream`).
- `NemotronSTTService` can follow the structure of Pipecat's Deepgram service
  (`pipecat/services/deepgram/stt.py`): send `Finalize` on the VAD stop, a
  `KeepAlive` every few seconds, reconnect on close.
- A standard Deepgram client works against Phonon too, which the official SDK
  test proves (`phonon/tests/test_deepgram_client.py`).

### Why a sub-service inside this repo (the DragonTTS pattern)

- Own `pyproject.toml` / `uv.lock`, so torch and NeMo never enter Clairvoyance's lock.
- Root `.dockerignore` **must** exclude `phonon/`. The root Dockerfile does `COPY . .`,
  and the exclusion is load-bearing (see the comment above `dragontts/` in `.dockerignore`).
- Root `pyproject.toml` → `[tool.pyrefly] project-excludes` gets `"phonon"`.
- Root CI still runs `black` / `isort` on `.`, so this code must pass them.
- Own image workflow `.github/workflows/phonon-docker-publish.yml` (copy of
  `dragontts-docker-publish.yml`, `paths: phonon/**`). Manual kubectl rollout.

| | DragonTTS | `phonon` |
|---|---|---|
| Processes | `uvicorn --workers 4` | **1 model process + N front-end processes** per GPU (§3). Never `--workers` for the model |
| Base image | `python:3.12-slim` | Python slim + PyTorch from pip (CUDA libraries come with it); the GPU driver comes from the node. Not tied to one GPU model |
| Shutdown | ~100 s, flips Clairvoyance's kill switch | Stop accepting, **finish open utterances**, close with 1001; the client reconnects elsewhere |
| Heavy deps | none | `torch`, `torchaudio`, `nemo_toolkit[asr]`, Silero in an **optional `[gpu]` extra** |

With the `[gpu]` extra, `uv sync` on a Mac installs only light deps, and front-ends,
protocol, IPC, admission and metrics run against a **fake engine** without a GPU.

---

## 2. The model (measured facts)

- NeMo `EncDecRNNTBPEModelWithPrompt`, ~638M params, 24 FastConformer layers,
  d_model 1024, RNNT decoder, language prompt `auto` (`hi-IN` to force Hindi).
- Cache-aware streaming, `att_context_size = [56, 6]` → **560 ms of new audio per step**,
  80 ms encoder frames. `RIGHT_CONTEXT` 0/1/3/6/13 trades latency for accuracy.
- **First chunk is different:** 49 frames, no pre-encode cache, `drop_extra_pre_encoded=0`.
  **Final step** uses `keep_all_outputs=True`. Neither can share a batch with normal steps.
- Encoder caches: `cache_last_channel[L, N, 56, 1024]`, `cache_last_time[L, N, 1024, 8]`.
- Preprocessor `normalize: NA`, dither off in eval → features computed on a short window;
  output is **deterministic** (100% identical transcripts at 50 sockets vs 8).
- **fp32 only for now.** 16-bit broke streaming. bf16 encoder only after held-out WER is unchanged.
- VRAM: **11 GB idle → 16 GB at 500 streams**.
- No Kannada support: Kannada speech comes out in Devanagari.
- Accuracy: real WER 15.0% / 16.5% on held-out calls, 11.8% IndicVoices. Weak on 1–2-word
  replies and spoken numbers.

---

## 3. Server architecture

**The one rule: the thread that feeds the GPU does nothing but feed the GPU.**
One batched step for 200 streams takes 90 ms. The first single-process server took 2.3 s
for the same step because socket handling and VAD were fighting it for the GIL.

```
        Clairvoyance (NemotronSTTService)                   one WebSocket per call
                               │
            port 8098 ── SO_REUSEPORT inside the pod / Service across pods
          ┌──────────────┬─────┴────────┬──────────────┐
   ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐
   │ front-end 1│ │ front-end 2│ │     ...    │ │ front-end N│  N = 4..8, no model, own GIL
   │ WS, VAD,   │ │            │ │            │ │            │  accept WS, VAD, JSON replies
   │ JSON       │ │            │ │            │ │            │
   └──────┬─────┘ └──────┬─────┘ └──────┬─────┘ └──────┬─────┘
          │  ONE batched IPC message per front-end every 10 ms
          └──────────────┴──────┬───────┴──────────────┘
                  ┌─────────────▼──────────────────────────────┐
                  │ MODEL PROCESS — exactly one per GPU         │
                  │  ingest threads (1 per front-end)           │
                  │  GPU feeder thread (the only torch caller)  │
                  │    continuous batching, CUDA graphs         │
                  └─────────────────────────────────────────────┘
```

| Component | Count | Owns | Must never |
|---|---|---|---|
| Front-end process | `min(8, max(4, ceil(peak/100)))` | WebSockets, framing, VAD, JSON | load the model, call CUDA |
| Model process | **exactly 1 per GPU** | weights, caches, hypotheses, GPU loop | handle sockets, run per-frame Python |
| GPU feeder thread | 1 | every torch call | wait on I/O, loop over streams in Python |
| Ingest threads | 1 per front-end | unpack batched messages, append audio, open/close turns | do model work |

The model process starts first; front-ends are spawned with the **`spawn`** context
(never `fork` after CUDA is initialised).

### The GPU loop

```python
while True:
    items = collect_ready()        # utterances with a full 560 ms chunk (+4-frame margin) or a pending final
    if not items:
        wake.wait(0.005); continue
    for (first, last), group in group_by_step_kind(items):   # ≤4 groups
        run_group(group, first, last)                         # ONE batched call per group
    emit_results_batched()         # one message per front-end per tick
```

1. **Continuous batching.** No timer, no waiting to fill a batch. Batches grow under load (2 → 13 → 60 → 111).
2. **Group by step kind** (first/later × final/not).
3. **Whole-tensor batch building.** No Python `for` over streams inside a step.
4. **Preallocated slot tensors** for caches; `index_select` in, `index_copy_` out.
5. **CUDA graphs** per batch-size bucket (8…512); warm up every bucket at startup.
6. **RNNT:** `greedy_batch`, `loop_labels=True`, `use_cuda_graph_decoder=True`.
7. **No host syncs** in the loop; pinned buffers, `non_blocking=True`, ≤1 sync per tick.
8. Detokenize only changed hypotheses.

### Invariants

- Only the feeder thread touches model state. A stream appears at most once per group, so its chunks run in order.
- Results go back to the owning front-end and socket only. There is no shared map that could mix calls.
- A closed stream is skipped next tick; late results are dropped; its slot is freed.
- One failing group never kills the feeder. Catch per group and reset only the affected streams.

---

## 4. Protocol

| Endpoint | Who uses it | Shape |
|---|---|---|
| `/v1/listen` | Clairvoyance's `NemotronSTTService`; also our tools and tests | **Deepgram-style**: query params, binary audio, `Results` (`is_final`, `speech_final`, `from_finalize`), `SpeechStarted`, `UtteranceEnd`; accepts `Finalize`, `KeepAlive`, `CloseStream` |

- Accept 8–48 kHz PCM16 in 20–100 ms frames; recommend **100 ms**.
- Every partial carries `audio_end_s`; every final carries `speech_end_s`, `detect_s`, `server_ms`.
- Ping 20 s / timeout 60 s.

| Close code | Meaning | Client action |
|---|---|---|
| 1000 | Normal end | nothing |
| 1001 | Shutting down (deploy) | reconnect, lands on another pod |
| 1008 | Auth / bad config | no retry; Sarvam for this call; alert |
| 1011 | Server error | reconnect once, then Sarvam |
| 1013 | Over the admission cap | retry once (another pod), then Sarvam |

---

## 5. Endpointing

- Phase 1: Silero on the front-end, **one instance per connection** (a shared instance
  crashes), ~80% of front-end CPU.
- Phase 2: **batched Silero on the GPU** in the model process, state `[2, MAX_STREAMS, 128]`,
  one call every 32 ms.
- Audio is forwarded **immediately**; VAD runs on its own 64 ms cadence and is sent as events.
- Per-tenant: min silence **500 ms**, threshold 0.5, pre-roll **300 ms**, max turn **30 s**.
  The silence wait is ~0.5 s of the ~0.6–0.8 s final lag.
- A client `Finalize` always forces a final now.

---

## 6. Capacity (measured on one RTX 5090, fp32)

| Item | Value |
|---|---|
| Planned concurrency per GPU | **350–400 continuously talking streams** (partial-lag p95 ≤ 0.35 s) |
| Burst ceiling | 500 streams (0 failures, p95 0.49 s, spikes 2–3 s) |
| Equivalent voice-agent calls | ≈ 780–890 per GPU at a 45% caller talk ratio (estimate) |
| GPU theoretical ceiling | ≈ 1,400 streams |
| Front-end CPU | ≈ 1.6% of a core per talking socket |
| Model process CPU | 1 core pinned at ~105%: **the current limit** |

- **GPU utilisation lies.** It fell as load rose. Watch `stt_feeder_busy_ratio`, batch size, step time.
- Past 8 front-ends it gets worse. To go further, make the feeder cheaper, don't add processes.
- **5090 numbers only.** The deployment GPU must be measured (Work-1 launch checklist and the pilot) before any budget.
- **Node CPU matters.** 400 sockets ≈ 6.4 front-end cores + 1 model core: almost all of an
  8-vCPU node. GPU VAD (PR-11) removes ~80% of front-end CPU; size nodes after it.

---

## 7. Configuration

Every setting is read from the environment with the `STT_` prefix (e.g. `STT_PORT`), so a platform-injected `PORT` can never change ours.

| Setting | Default | Notes |
|---|---|---|
| `MODEL_PATH` | GCS path in prod, local path in dev | rollout = change path; rollback = previous path |
| `ENGINE` | `nemo` | `fake` for Mac tests |
| `LANG_PROMPT` | `auto` | `hi-IN` to force Hindi |
| `RIGHT_CONTEXT` | `6` (560 ms) | 0/1/3/6/13 |
| `PORT` | `8098` | front-ends share it |
| `FRONTENDS` | `0` = auto | `min(8, max(4, ceil(PLAN_SOCKETS/100)))` |
| `PLAN_SOCKETS` | `400` | also the admission cap, set per GPU after measuring |
| `VAD_MIN_SILENCE_MS` / `PREROLL_MS` / `MAX_UTT_S` | `500` / `300` / `30` | per-tenant override |
| `FLUSH_MS` | `10` | front-end → model batching interval |
| `MAX_BATCH` | `512` | largest CUDA-graph bucket |

## 8. Metrics and alerts

`stt_batch_size`, `stt_step_ms`, **`stt_feeder_busy_ratio`**, `stt_partial_lag_seconds`,
`stt_final_server_seconds`, `stt_streams_open`, `stt_streams_speaking`,
`stt_rejected_total{code}`, `stt_frontend_cpu_ratio{fe}`, GPU util, VRAM, `stt_finals_total`,
`stt_empty_finals_total`.

Alert: partial-lag p95 > 0.5 s for 1 min · feeder busy > 95% for 5 min · any 1013.
Logs carry `call_id` / stream id; never tokens or raw audio.

## 9. Server layout

```
phonon/                project: pyproject.toml  uv.lock  README.md  .env.example  tests/  tools/
  stt/                 the package (`python -m stt`)
    supervisor.py  config.py  ipc.py  log.py  proc.py  metrics.py  auth.py
    protocol/  deepgram.py
    frontend/  main.py  app.py  session.py  model_link.py  vad.py
    model/     main.py  ingest.py  streams.py  scheduler.py  engine.py  fake_engine.py  nemo_engine.py  vad_gpu.py
```

---

## 10. All the work

Each PR is one commit (CI enforces it) and gets its own doc in `prs/` when we start it.
"Work" items are operations or infrastructure, not code PRs.

### Part A — STT server (`phonon/`)

| # | What | Done when | Doc |
|---|---|---|---|
We start from the **skeleton of the architecture** and then solve **one problem per PR**.
Each PR leaves the server working end to end; later PRs replace a piece, never restructure it.

| # | Problem it solves | Done when | Doc |
|---|---|---|---|
| PR-01.1 | **Core**: engine contract, fake engine, stream table (turns, chunks, finals, overload), IPC message vocabulary, logging and crash safety nets, repo wiring and CI | Table and engine behaviour pinned by tests; no processes yet | [pr-01.1](prs/pr-01-1-core.md) |
| PR-01.2 | **Model process**: settings, IPC transport, feeder and writers, ingest, model process entry point | The model side works end to end over IPC | [pr-01.2](prs/pr-01-2-model-process.md) |
| PR-01.3 | **Front-end + supervisor**: protocol, WebSocket front-end, supervisor, end-to-end and failure tests, tools | A socket sends audio and gets fake text back through the full path, on a Mac | [pr-01.3](prs/pr-01-3-frontend-supervisor.md) |
| PR-02 | **Clairvoyance-ready protocol**: the Deepgram endpoint itself landed in PR-01.3; this adds `resume` + a replay allowance in the rate limit (reconnecting clients resend up to 30 s), `SpeechStarted` / `UtteranceEnd` if templates need them, | `NemotronSTTService` (PR-15) runs a call against the fake engine, including a reconnect and replay | later |
| PR-03 | **Real transcripts**: load the model, batched cache-aware step grouped by step kind (first/later × final/not), batched features, `[gpu]` extra; CUDA errors raised as `FatalEngineError`; a bad stream fails only itself, not its batch | Batched output = one-stream-at-a-time output, token for token | later |
| PR-04 | **End of speech**: Silero per connection on the front-end, per-tenant silence/pre-roll/max-turn, finals on VAD end or `Finalize` | Finals arrive with the last word, no split mid-sentence on recorded calls | later |
| PR-05 | **We can measure**: `load_client.py` (staged real-audio load) and `score_run.py` (WER under load) | Baseline capacity, lag and WER recorded for this version | later |
| PR-06 | **Overload**: admission cap → 1013 and a load figure for autoscaling, rate limit, ping/timeout; shed load per socket before any whole connection (front-end `_pending` cap); streams that are behind catch up several chunks per round | Over-cap gets 1013 while accepted streams keep their SLA | later |
| PR-07 | **Deploys and liveness**: `/healthz`, `/readyz`, feeder heartbeat checked by the supervisor (a stuck model process exits), SIGTERM → finish open utterances → 1001, stop front-ends before the model, clean stale IPC folders | A restart under load drops no utterance; a stuck feeder restarts the pod | later |
| PR-08 | **Visibility**: Prometheus metrics and alert rules (feeder busy, lag, batch size, 1013s, empty finals) | Saturation visible before callers feel it | later |
| PR-09 | **Per-step CPU cost**: preallocated slot tensors for caches, whole-tensor batch building, no host syncs, binary IPC framing instead of pickle | Identical transcripts; lower step time and model-process CPU at the same load | later |
| PR-10 | **Kernel launch cost**: CUDA graphs per batch-size bucket, warm-up at startup; TF32 as an experiment | Identical transcripts; staged test reaches 800+ sockets | later |
| PR-11 | **Front-end CPU**: batched Silero on the GPU, endpointing logic torch-free | Same VAD decisions; front-end CPU drop measured | later |
| PR-12 | **Long calls**: ring buffers, nothing grows with call length, optional audio recording | 1 h soak with ≥30 min calls, flat VRAM and RSS | later |
| PR-13 | **Who may connect**: auth checked once at connect (Google ID token as the Deepgram key, or Secret Manager tokens) | Only Clairvoyance identities open a stream | later |
| PR-14 | **Unsupported languages**: Kannada and others never reach Nemotron silently | Covered by a test on both paths | later |

### Part B — Infrastructure and pilot

| # | What | Done when |
|---|---|---|
| Work-1 | Launch checklist (design guide §10) on the candidate deployment GPU | Capacity, `PLAN_SOCKETS`, node sizing and cost per call are measured |
| Work-2 | GPU quota in asia-south1, GPU node pool, Artifact Registry repo, GCS model bucket, image workflow (only when explicitly approved) | Resources exist; image builds on `release` |
| Work-3 | Kubernetes deployment: one pod per GPU, internal-only Service, NetworkPolicy (only Clairvoyance pods), no 30 s load-balancer cut on WebSockets | Clairvoyance pods can reach it; nothing else can |
| Work-4 | Pilot: one GPU node, rerun the checklist there, 24 h soak at 80% load, deploy during load | Real per-GPU numbers; no dropped calls on deploy |

### Part C — Clairvoyance integration

| # | What | Done when |
|---|---|---|
| PR-15 | **Client + provider wiring**: `NemotronSTTService` (`app/ai/voice/stt/nemotron.py`, a Pipecat STT service speaking Phonon's protocol), `STTProvider.NEMOTRON` → it, `NEMOTRON_STT_URL` and credential in config, per-template selection and endpointing values validated at template save | A test template runs a full call on Nemotron |
| PR-16 | **Resilience**: Sarvam fallback on 1008 / repeated 1011 / 1013 for the rest of the call, kill switch like DragonTTS's | Server down or full → calls continue on Sarvam |
| PR-17 | **Shadow mode**: send the same audio to Nemotron while Sarvam's text drives the call; log both | Running on a slice of traffic |
| Work-5 | Compare shadow results: real-call WER and latency vs Sarvam over several days | Go / no-go decision |

### Part D — Rollout

| # | What | Done when |
|---|---|---|
| Work-6 | Gradual per-template / per-merchant switch, autoscaling on open streams and feeder busy, alerts live | Stable error rates, no dropped calls on deploys |
| Work-7 | Model updates: new version → GCS path → change `MODEL_PATH` → rolling update; rollback = previous path | A version rolled out and back without code changes |

### Part E — Later

| # | What |
|---|---|
| PR-18 | **ITN**: spoken numbers → digits on finals (server or client, to decide) |
| Work-8 | Training for weak spots: short replies, numbers, names |
| Work-9 | bf16 / mixed precision once held-out WER is unchanged → more streams per GPU |

---

## 11. Decisions

| Decision | Choice | Status |
|---|---|---|
| Home | `phonon/` in this repo, DragonTTS pattern | **Decided** |
| Process model | 1 model process + 4–8 front-ends per GPU | **Decided (measured)** |
| Batching | Continuous, no timer | **Decided (measured)** |
| Protocol | Deepgram-compatible `/v1/listen` only (built in PR-01.3); tests and tools use it too, so they exercise what production uses | **Decided** |
| Final | `Finalize` or VAD end → final step with `keep_all_outputs=True` | **Decided** |
| Shutdown | Finish open utterances, then 1001 | **Decided** |
| Precision | fp32; bf16 only after WER parity | **Decided for now** |
| PR order | Skeleton first, then one problem per PR | **Decided** |
| Base image | Python slim + pip torch; driver from the node | Proposed |
| Auth | Google ID token as the Deepgram key, else Secret Manager tokens; plus NetworkPolicy | Proposed (PR-13) |
| Language routing | Template language picks the provider in Clairvoyance | Proposed (PR-14) |
| Deployment GPU | L4 or other, chosen by measured cost per stream | Open (Work-1 / pilot) |

## 12. Open checks

- How `NemotronSTTService` should react to each close code (1001 / 1011 / 1013): reconnect and replay, or Sarvam (PR-15/16).
- Whether training call audio was 8 kHz upsampled to 16 kHz, and with which resampler (PR-03).
- Token expiry on reconnects during calls longer than 1 h (PR-13).
- CI disk space for a torch image on `ubuntu-latest` (Work-2).

## 13. Mistakes already made (don't repeat)

Full table in [reference/load-test-design-guide.md §8](reference/load-test-design-guide.md).
One process for everything · Python loops over streams in a step · trusting `nvidia-smi`
util · planning `--workers N` · guessing the front-end count · lag measured without the
4-frame margin · holding audio for VAD · shared Silero · keeping all audio of a call ·
`WebSocket` imported inside a function with `from __future__ import annotations` (→ HTTP 403) ·
scoring Kannada against Kannada-script references.

## 14. Rules

- The RTX 5090 is also a production machine (SraVaani :8000, voice-cell STT/TTS
  containers, :8090, the display). Check free GPU/RAM first; never evict those services;
  never run training and a load test together; cap container memory (`--memory`).
- No cloud-provider actions unless explicitly asked; confirm anything irreversible.
- Never print secrets (Sarvam key, tokens).
