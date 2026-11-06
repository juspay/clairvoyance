# PR-01.3 — Front-end + supervisor

Back to [main plan](../main.md) · previous parts: [01.1 Core](pr-01-1-core.md), [01.2 Model process](pr-01-2-model-process.md) · full design: [Phonon design doc](https://claude.ai/artifact/CiNPw3DLD1Gr2HdpbK7vkP)

Completes PR-01: the WebSocket front-ends and the supervisor that runs
everything. After this PR `python -m stt` is a working server (fake engine).

## What this PR adds

| File | What it does |
|---|---|
| `phonon/stt/protocol/deepgram.py` | **Phonon's protocol, modelled on Deepgram's live format** (Clairvoyance's `NemotronSTTService` will speak it): connect params from the URL, `Finalize` / `KeepAlive` / `CloseStream`, `Results` and `Metadata` with every field the Deepgram SDK requires, errors as close code + reason. Strict validation (allow-listed sample rates, strict ints, `call_id` pattern) |
| `phonon/stt/frontend/app.py` | The `/v1/listen` route: the connection is the start; then audio, `Finalize`, `KeepAlive`, `CloseStream` |
| `phonon/stt/frontend/session.py` | One socket's life: `new → starting → ready → closing → closed`. One sender task (the only writer to the socket); rate limit (~1.5× real time + 5 s burst); finalize flood limit; idle timeout (`KeepAlive` keeps a silent call open); `CloseStream` waits for finals already asked for, then `Metadata` |
| `phonon/stt/frontend/model_link.py` | The front-end's IPC: a flush thread (audio batched every 10 ms, finalize/close sent at once) and a receiver thread (hands results to the event loop). If the model is lost: every socket closed with 1011, then exit 75 |
| `phonon/stt/frontend/main.py` | Front-end process entry: binds :8098 with `SO_REUSEPORT`, connects to the model, runs uvicorn |
| `phonon/stt/supervisor.py` | Starts the model first, then the front-ends; restarts a crashed front-end with backoff; a crash loop or the model's death ends the pod; front-ends stop before the model on shutdown |
| `phonon/stt/__main__.py` | `python -m stt` |
| `phonon/stt/config.py` | Adds the front-end and supervisor settings |
| `phonon/tools/stream_wav.py` | Stream a WAV to a running server and print what comes back |

Dependencies added: `fastapi`, `uvicorn[standard]`, `websockets`, `orjson`; test-only: `deepgram-sdk==6.1.1` (the official SDK, as a reference client).

**Why this protocol now:** the tests and tools exercise the protocol production uses. There is no second protocol to keep in step.

## Failure behaviour (all tested end to end)

| What fails | What happens |
|---|---|
| A bad connect URL, an unknown message, too fast, finalize flood | That one socket closes (1008, with a reason); others are untouched |
| A stream sends nothing for 20 s (no audio, no `KeepAlive`) | Closed 1011 `idle` |
| A front-end crashes | Only its sockets close; it restarts with backoff; 3 crashes in 60 s ends the pod |
| A front-end loses the model (shed under backpressure) | Exit 75: restarted without counting as a crash (own pod-wide limit) |
| The model process dies | Front-ends close every socket with 1011 and exit; the supervisor exits 1 (Kubernetes restarts the pod) |

## Tests (88 total, ~25 s, all on a Mac)

| File | Covers |
|---|---|
| `tests/test_deepgram_client.py` | **The official Deepgram SDK** connects, sends media / KeepAlive / Finalize / CloseStream, and parses every partial, the final (`is_final`, `speech_final`, `from_finalize`, `start`, `duration`) and `Metadata` |
| `tests/test_end_to_end.py` | Real processes over `/v1/listen`: partials → final → fresh utterance; back-to-back turns keep boundaries and `seq`; CloseStream delivers owed finals then Metadata then 1000; empty final; 20 sockets at once; bad connect URLs (1008 + reason); unknown message; idle timeout vs KeepAlive; 64 KB limit (1009); rate limit; finalize flood |
| `tests/test_failures.py` | Kill a front-end; kill the model (1011, exit 1); crash loop ends the pod |
| `tests/test_protocol.py` | Connect-param and message validation; `coalesce` (audio never merged across a finalize) |
| `tests/test_model_link.py` | Results after the event loop closed are dropped, not a crash |
| `tests/conftest.py` | Runs the real server as a subprocess, outside `phonon/` so a local `.env` can't change results |

```bash
cd phonon && uv sync --extra dev && uv run pyrefly check && uv run pytest -q
uv run python -m stt          # then, in another terminal:
uv run python tools/stream_wav.py some_8k_mono.wav
```

All tests use one front-end: macOS doesn't spread `SO_REUSEPORT` connections.
One Linux run with `STT_FRONTENDS=4` should confirm several front-ends share
the port.
