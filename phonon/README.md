# Phonon

Self-hosted streaming STT server for Clairvoyance (named after the quantum of
sound). The server is model-agnostic: Nemotron first, other models later.
Design: `docs/phonon/main.md` in the repo.

**Status: PR-1 of 3 — the core.** No processes or sockets yet. This PR has:

| File | What it is |
|---|---|
| `stt/model/engine.py` | The engine contract every model follows (`chunk_ms`, `open` / `step` / `release`, `FatalEngineError`) |
| `stt/model/fake_engine.py` | A deterministic engine with no model, for tests |
| `stt/model/streams.py` | The stream table: per-call audio, turn boundaries, chunk cutting, finals, overload drops |
| `stt/ipc.py` | The message vocabulary between front-ends and the model process |
| `stt/log.py`, `stt/proc.py` | One log format; crash safety nets for processes |

Next: PR-2 adds the model process (feeder, IPC transport, ingest); PR-3 adds the
WebSocket front-end and the supervisor.

## Run the tests

```bash
uv sync --extra dev
uv run pyrefly check
uv run pytest -q
```
