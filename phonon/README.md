# Phonon

Self-hosted streaming STT server for Clairvoyance (named after the quantum of
sound). The server is model-agnostic: Nemotron first, other models later.
Design: `docs/phonon/main.md` in the repo.

**Status: PR-01 complete (3 of 3).** The full server runs with a fake engine
(deterministic text such as `s1c0 s1c1`). The real Nemotron engine is PR-03 and
plugs in behind `stt/model/engine.py`.

```
supervisor ─┬─ model process   feeder (only engine caller), ingest + writer per front-end
            └─ front-end ×N    WebSockets on :8098 (SO_REUSEPORT), one IPC connection each
```

## Run

```bash
uv sync --extra dev
uv run pyrefly check
uv run pytest -q                                   # ~25 s
uv run python -m stt                               # ws://127.0.0.1:8098/v1/listen
uv run python tools/stream_wav.py some_8k_mono.wav # 8/16/24/48 kHz, 16-bit mono
```

On Linux you can run several front-ends on one port: `STT_FRONTENDS=4`.
Settings are `STT_*` environment variables (see `.env.example`). The server binds
`127.0.0.1` by default; reach it from another machine with an SSH tunnel
(`ssh -L 8098:localhost:8098 user@host`), not by binding `0.0.0.0`: there is no
auth until PR-13.

## Protocol: Deepgram-style (`/v1/listen`)

Clairvoyance connects with its own `NemotronSTTService`. The protocol
(`stt/protocol/deepgram.py`) follows Deepgram's live format, so that client stays
small and a standard Deepgram client can connect too:

```
connect  ws://host:8098/v1/listen?encoding=linear16&sample_rate=8000&channels=1[&call_id=C123]
         (the connection is the start; other Deepgram parameters are accepted and ignored)
C→S <binary PCM16>                 e.g. 100 ms per message
C→S {"type":"Finalize"}            end of the caller's turn  ({"seq":N} optional, echoed back)
C→S {"type":"KeepAlive"}           keeps a silent stream open (idle timeout 20 s)
C→S {"type":"CloseStream"}         finals already asked for are sent, then Metadata, then close 1000
S→C {"type":"Results","is_final":false,...}                                    partial
S→C {"type":"Results","is_final":true,"speech_final":true,"from_finalize":true,...}  final
S→C {"type":"Metadata",...}        after CloseStream
```

Each `Results` holds the **whole utterance text** in `channel.alternatives[0].transcript`,
with `start` / `duration` in seconds on the stream's clock, and one final per
utterance. Phonon adds `utt`, `seq` and `server_ms` as extra fields. Errors come
as a close code with a reason (`<code>: <message>`):

| Close code | Meaning | Client should |
|---|---|---|
| 1000 | Normal end | — |
| 1008 | Bad connect URL, bad message, too fast, too many finalizes | Fix the client; don't retry as is |
| 1009 | Message over 64 KB | Send smaller frames |
| 1011 | Server error, model unavailable, idle, finals lost | Reconnect (another pod) and replay |
