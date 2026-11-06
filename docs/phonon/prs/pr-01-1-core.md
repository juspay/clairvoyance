# PR-01.1 — Core

Back to [main plan](../main.md) · full design and reasoning: [Phonon design doc](https://claude.ai/artifact/CiNPw3DLD1Gr2HdpbK7vkP)

PR-01 is split into three reviewable parts:

| Part | Contents |
|---|---|
| **01.1 Core (this PR)** | Engine contract, fake engine, stream table, IPC message vocabulary, logging, crash safety nets, repo wiring + CI |
| 01.2 Model process | Settings, IPC transport, feeder + writers, ingest, model process entry |
| 01.3 Front-end + supervisor | Protocol, WebSocket front-end, supervisor, end-to-end and failure tests, tools |

## What this PR adds

Pure logic: no processes, no sockets, one runtime dependency (`loguru`).

| File | What it does |
|---|---|
| `phonon/stt/model/engine.py` | The contract every engine follows: `chunk_ms` (the model's step size), `open(key)` / `step(kind, items)` / `release(key)`, `FatalEngineError`. A step returns the **whole utterance text**, which replaces the old text (RNNT word pieces continue across steps, so text is never joined) |
| `phonon/stt/model/fake_engine.py` | Deterministic engine with no model: one token `s<sid>c<n>` per chunk, reset after a final. Refuses mixed step kinds and unopened streams, as the real model would |
| `phonon/stt/model/streams.py` | The stream table (below) |
| `phonon/stt/ipc.py` | Message kinds between front-ends and the model process, and the shared `SAMPLE_RATES` allow-list. The transport comes in 01.2 |
| `phonon/stt/log.py` | One log format for every process; enqueued, so busy threads never wait on stderr |
| `phonon/stt/proc.py` | A critical thread dying exits its process; a process exits when its supervisor is gone; `exit_now()` flushes logs first; `EXIT_MODEL_LOST` (75) for front-ends that lost the model (used in 01.3) |

Outside `phonon/`:
- `.dockerignore`: excludes `phonon/` (the root Dockerfile does `COPY . .`).
- `pyproject.toml`: pyrefly excludes `phonon` (it has its own venv and type check).
- `.github/workflows/pr-build-check.yml`: new step runs Phonon's own pyrefly and pytest, so the two lines above are enforced in CI.

## The stream table — what to review

One row per live call, keyed by `(conn_id, sid)`; written by ingest threads, read by the feeder (both in 01.2).

| Behaviour | How |
|---|---|
| Chunks | A full chunk (`chunk_ms` from the engine, 560 ms for Nemotron) steps normally; whole samples only (an odd byte waits for the next append) |
| Turn boundaries | Each `finalize` records a byte position (`FinalMark`); a final takes audio only up to it, so the next turn's audio never leaks in, and every finalize gets its own final |
| Finals | A remainder shorter than a chunk ends the turn as a final; a finalize exactly at a chunk boundary is one step, not two |
| Max utterance | A turn with no finalize is cut by a forced final at `max_utterance_s` (`forced: true`, `server_ms: null`) |
| Overload | Pending audio is capped (`max_pending_s`); the oldest audio is dropped, each turn is charged only for its own lost audio, finalize marks move with the drop, and the caller is told once per episode |
| Timestamps | A stream clock counts samples in stream order (stepped or dropped); a result reports the clock at the end of its own chunk, so a drop during a step can't move it |
| Removal | `close` only marks a row; the feeder removes it next cycle and releases engine state. A stream opened and closed in the same cycle never touches the engine |
| Per-cycle state | What `apply()` needs (end clock, the finalize answered) travels in the cycle's `ChunkReceipt` records, never left on the row |
| Locking | One lock, held only for bookkeeping; never during an engine step, never around I/O or logging |

## Tests (25, all on a Mac, < 1 s)

| File | Covers |
|---|---|
| `tests/test_streams.py` | Every row above, including the review scenarios: next turn's audio after a finalize, a finalize during a final in flight, drops across finalize marks and during a step |
| `tests/test_fake_engine.py` | Whole-text results, reset after final, refusals, release |
| `tests/test_proc.py` | A dead thread takes its process down (and its log line is flushed) |
| `tests/test_repo_wiring.py` | `.dockerignore` and the pyrefly exclude (skipped if copied outside the repo) |

```bash
cd phonon
uv sync --extra dev
uv run pyrefly check       # 0 diagnostics, warnings included
uv run pytest -q           # 25 passed
```

## Not in this PR

Settings, IPC transport, feeder, ingest, model process (01.2); protocol,
front-end, supervisor, end-to-end tests, tools (01.3). The real Nemotron engine
is PR-03.
