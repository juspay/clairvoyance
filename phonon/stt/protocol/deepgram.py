"""Phonon's protocol on /v1/listen, modelled on Deepgram's live API.

Clairvoyance connects with its own NemotronSTTService. The message shapes
follow Deepgram's live format (a well-known, documented shape for streaming
STT), so the client side stays small and a standard Deepgram client can
connect too (tests/test_deepgram_client.py keeps that true):

    connect  /v1/listen?encoding=linear16&sample_rate=8000&channels=1&...
             (the connection is the "start"; unknown parameters are ignored)
    C→S <binary PCM16>
    C→S {"type":"Finalize"}       end of the caller's turn (e.g. on the client's VAD stop)
    C→S {"type":"KeepAlive"}      every 5 s; nothing to do
    C→S {"type":"CloseStream"}    finish the finals already asked for, then close
    S→C {"type":"Results", "is_final":false, ...}                    partial
    S→C {"type":"Results", "is_final":true, "speech_final":true,
         "from_finalize":true, ...}                                  final
    S→C {"type":"Metadata", ...}  after CloseStream, before the close frame

Errors are reported the Deepgram way: a close code and a reason text.

Every Results message carries all fields Deepgram's format requires (the
Deepgram SDK parses each one into a typed ListenV1Results). Phonon adds `utt`,
`seq` and `server_ms` as extra fields, which Deepgram clients keep or ignore.
"""

import re
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StrictInt, TypeAdapter, ValidationError
from stt import ipc

PATH = "/v1/listen"
MODEL_INFO = {"name": "phonon", "version": "0.1.0", "arch": "fake"}
_CALL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class StreamConfig:
    """What a client asks for in the connect URL's query string."""

    sample_rate: int
    call_id: str


def parse_query(params: dict[str, str]) -> StreamConfig:
    """Validate the connect parameters Phonon uses; ignore the rest (model,
    language, punctuate, ... are Deepgram options with no meaning here)."""
    encoding = params.get("encoding", "linear16")
    if encoding != "linear16":
        raise ProtocolError(f"encoding must be linear16, not {encoding!r}")
    if params.get("channels", "1") != "1":
        raise ProtocolError("only mono audio (channels=1) is supported")
    if params.get("multichannel", "false") not in ("false", "False"):
        raise ProtocolError("multichannel is not supported")
    rate_text = params.get("sample_rate", "")
    if not rate_text.isdigit() or int(rate_text) not in ipc.SAMPLE_RATES:
        allowed = ", ".join(str(r) for r in sorted(ipc.SAMPLE_RATES))
        raise ProtocolError(f"sample_rate must be one of {allowed}")
    # call_id is a Phonon extension for log correlation; letters, digits and
    # . _ : - only, so a newline can't forge log lines.
    call_id = params.get("call_id", "-")
    if not _CALL_ID.match(call_id):
        raise ProtocolError("call_id may use letters, digits and . _ : - only")
    return StreamConfig(int(rate_text), call_id)


class Finalize(BaseModel):
    type: Literal["Finalize"]
    # Phonon extension: an optional turn id, echoed on the final.
    seq: StrictInt | None = Field(None, ge=0, le=2_147_483_647)


class KeepAlive(BaseModel):
    type: Literal["KeepAlive"]


class CloseStream(BaseModel):
    type: Literal["CloseStream"]


ClientMessage = Annotated[
    Finalize | KeepAlive | CloseStream, Field(discriminator="type")
]
_client = TypeAdapter(ClientMessage)


def parse_client(text: str) -> Finalize | KeepAlive | CloseStream:
    try:
        return _client.validate_json(text)
    except ValidationError as exc:
        raise ProtocolError(exc.errors(include_url=False)[0]["msg"]) from None


def results(
    request_id: str,
    text: str,
    start_s: float,
    end_s: float,
    *,
    is_final: bool,
    speech_final: bool,
    from_finalize: bool,
    utt: int,
    seq: int | None = None,
    server_ms: int | None = None,
) -> dict:
    """A Deepgram `Results` message for one utterance.

    Phonon sends the WHOLE utterance text each time (partials grow, the final
    repeats it in full), and exactly one final per utterance, so a client that
    appends finals within a turn never doubles words.
    """
    return {
        "type": "Results",
        "channel_index": [0, 1],
        "duration": round(max(0.0, end_s - start_s), 3),
        "start": round(start_s, 3),
        "is_final": is_final,
        "speech_final": speech_final,
        "from_finalize": from_finalize,
        "channel": {
            "alternatives": [{"transcript": text, "confidence": 1.0, "words": []}]
        },
        "metadata": {
            "request_id": request_id,
            "model_info": MODEL_INFO,
            "model_uuid": "phonon",
        },
        # Phonon extensions (extra fields a Deepgram client keeps or ignores)
        "utt": utt,
        "seq": seq,
        "server_ms": server_ms,
    }


def metadata(request_id: str, duration_s: float) -> dict:
    """Deepgram's closing summary, sent after CloseStream."""
    return {
        "type": "Metadata",
        "transaction_key": "deprecated",
        "request_id": request_id,
        "sha256": "",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "duration": round(duration_s, 3),
        "channels": 1,
        "models": [],
        "model_info": {},
    }


def close_reason(code: str, message: str) -> str:
    """A WebSocket close reason must fit in 123 bytes."""
    return f"{code}: {message}".encode()[:123].decode(errors="ignore")
