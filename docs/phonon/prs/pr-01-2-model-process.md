# PR-01.2 — Model process

Back to [main plan](../main.md) · previous part: [PR-01.1 Core](pr-01-1-core.md) · full design: [Phonon design doc](https://claude.ai/artifact/CiNPw3DLD1Gr2HdpbK7vkP)

Builds on 01.1 (stream table, engine contract). Adds the model process — the
one process per GPU that holds every call's state — and the IPC it talks over.
Still no WebSockets: tests drive it over IPC exactly as a front-end will (01.3).

## What this PR adds

| File | What it does |
|---|---|
| `phonon/stt/config.py` | `Settings`: read once from `STT_*` env vars, passed to child processes as an argument (nothing reads the environment later). Only the model-side settings for now; 01.3 adds the front-end and supervisor ones |
| `phonon/stt/ipc.py` | Adds the transport: the model listens on a Unix socket in a private 0700 folder; each front-end connects. Each peer is authenticated (random per-run key) **on its own thread with a timeout**, so a stalled peer can't block others. `shutdown()` wakes a blocked `recv()` without closing (only the owning thread closes) |
| `phonon/stt/model/scheduler.py` | **The feeder** (the one thread that calls the engine): cut the next work → `open` new streams and send `ready` at once → `release` closed ones → one `step` per step kind → apply results. Plus **ConnectionWriter**, the only sender on each front-end connection (the feeder never does I/O) |
| `phonon/stt/model/ingest.py` | One reader thread per front-end connection: files `open` / `audio` / `finalize` / `close` into the stream table. A bad event closes only its own stream; an unreadable frame ends only that connection |
| `phonon/stt/model/main.py` | The process entry point: crash handlers, engine (`make_engine` picks it by `STT_ENGINE`), table, feeder thread, then accept front-ends for the life of the process |

Dependencies added: `pydantic`, `pydantic-settings`.

## Threads in the model process

```
main thread      accept front-end connections (ipc.serve)
handshake ×N     authenticate one peer each, with a timeout
ingest ×N        read one front-end connection → stream table
writer ×N        the only sender on one front-end connection
feeder ×1        the only caller of the engine (later: the GPU)
```

## What to review

| Area | Rule it keeps |
|---|---|
| Feeder | Never waits on I/O; results go to writer queues. Re-wakes itself while streams have work queued, so a burst drains at once. A `FatalEngineError` exits the process; any other step error closes only that group's streams |
| Writers | One sender per connection, so two threads never interleave bytes. A front-end that stops reading past `STT_CONN_OUTBOUND_MAX` is shed (shutdown), not allowed to stall the feeder |
| Ownership | The ingest thread owns its connection: it closes it only after its writer has stopped |
| Ingest | Validates every field (sid is an int, `sample_rate` in the allow-list, finalize payload shape); a bad event → that stream closed with an error |
| Accept | Transient `accept()` errors (e.g. out of file descriptors) are retried; if the listener dies the process exits loudly |

## Tests (47 total, ~1.5 s, all on a Mac)

| File | Covers |
|---|---|
| `tests/test_model_process.py` (new) | The real model process over real IPC: `ready` → partials → final with `seq`; two front-ends get only their own results; a wrong key is refused while good peers still connect |
| `tests/test_scheduler.py` | Feeder: step kinds never mixed, results to the owning connection only, failing group closes only its streams, fatal error exits, `ready` before the step, failed open never released, real feeder thread drains a burst |
| `tests/test_ipc_and_ingest.py` | `shutdown` wakes a blocked recv without closing; stalled handshake; bad events and bad sample rates; unreadable frames; writer stops at once |
| 01.1 tests | Unchanged |

```bash
cd phonon && uv sync --extra dev && uv run pyrefly check && uv run pytest -q
```

## Not in this PR

Protocol, WebSocket front-end, supervisor, end-to-end and failure tests, tools (01.3).
