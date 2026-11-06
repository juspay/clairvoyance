"""Stream a 16-bit mono WAV to a running Phonon over the Deepgram protocol, and
print each transcript as it comes back.

    uv run python tools/stream_wav.py call.wav
    uv run python tools/stream_wav.py call.wav --url ws://127.0.0.1:8098 --fast
"""

import argparse
import json
import sys
import threading
import time
import wave
from pathlib import Path
from urllib.parse import urlencode

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

# Run as a script from phonon/: make the `stt` package importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stt.ipc import SAMPLE_RATES  # noqa: E402
from stt.protocol import deepgram  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav")
    parser.add_argument("--url", default="ws://127.0.0.1:8098")
    parser.add_argument("--frame-ms", type=int, default=100)
    parser.add_argument("--fast", action="store_true", help="send as fast as allowed")
    args = parser.parse_args()

    with wave.open(args.wav, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise SystemExit("need 16-bit mono PCM")
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    if rate not in SAMPLE_RATES:
        allowed = ", ".join(str(r) for r in sorted(SAMPLE_RATES))
        raise SystemExit(f"unsupported sample rate {rate} Hz; use one of {allowed}")

    query = urlencode(
        {"encoding": "linear16", "sample_rate": rate, "channels": 1, "call_id": "wav"}
    )
    with connect(f"{args.url}{deepgram.PATH}?{query}") as ws:
        done = threading.Event()

        def reader() -> None:
            try:
                for message in ws:
                    msg = json.loads(message)
                    if msg.get("type") == "Results":
                        kind = "final  " if msg["is_final"] else "partial"
                        text = msg["channel"]["alternatives"][0]["transcript"]
                        print(
                            f"{kind} {msg['start']:6.2f}-{msg['start'] + msg['duration']:6.2f}s  {text}"
                        )
                        if msg["from_finalize"]:
                            done.set()
                    else:
                        print(message)
            except ConnectionClosed as exc:
                if exc.rcvd and exc.rcvd.code != 1000:
                    print(f"closed {exc.rcvd.code}: {exc.rcvd.reason}")
            done.set()  # the server closed the stream: nothing more will come

        threading.Thread(target=reader, daemon=True).start()
        step = rate * args.frame_ms // 1000 * 2
        try:
            for i in range(0, len(pcm), step):
                ws.send(pcm[i : i + step])
                if not args.fast:
                    time.sleep(args.frame_ms / 1000)
            ws.send(json.dumps({"type": "Finalize"}))
            done.wait(10 + 2 * len(pcm) / (rate * 2))
            ws.send(json.dumps({"type": "CloseStream"}))
            done.wait(5)
        except ConnectionClosed as exc:
            raise SystemExit(f"the server closed the stream: {exc}") from None


if __name__ == "__main__":
    main()
