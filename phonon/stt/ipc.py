"""The messages front-ends and the model process exchange.

Messages travel as batches (lists) of events. Client data travels only as
validated fields and raw bytes, never as client-built objects.

    front-end → model:  (OPEN, sid, {"sample_rate"})
                        (AUDIO, sid, bytes)
                        (FINALIZE, sid, {"seq": int | None, "t": float})
                        (CLOSE, sid, None)
    model → front-end:  (OPENED, sid, None)          only after engine.open succeeded
                        (PARTIAL, sid, {"text", "audio_end_s", "utt"})
                        (FINAL, sid, {"text", "speech_end_s", "server_ms",
                                      "utt", "seq", "forced"})
                        (WARNING, sid, {"code", "message"})
                        (ERROR, sid, {"code", "message"})

The transport (Unix socket, handshake, shutdown) is added with the model
process in the next PR.
"""

from typing import Any

OPEN = "open"
AUDIO = "audio"
FINALIZE = "finalize"
CLOSE = "close"

OPENED = "opened"
PARTIAL = "partial"
FINAL = "final"
WARNING = "warning"
ERROR = "error"

Event = tuple[str, int, Any]

# Sample rates a stream may use; checked by the front-end AND the model process.
SAMPLE_RATES = frozenset({8000, 16000, 24000, 48000})
