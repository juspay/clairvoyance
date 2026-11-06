"""The real server (supervisor + model + front-end) over Phonon's Deepgram-style
protocol, the way Clairvoyance's NemotronSTTService will use it."""

import json
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

RATE = 8000
FRAME = RATE // 10 * 2  # 100 ms of PCM16


def send_audio(ws: ClientConnection, seconds: float) -> None:
    for _ in range(round(seconds * 10)):
        ws.send(b"\0" * FRAME)


def finalize(ws: ClientConnection, seq: int | None = None) -> None:
    msg: dict = {"type": "Finalize"}
    if seq is not None:
        msg["seq"] = seq
    ws.send(json.dumps(msg))


def text(msg: dict) -> str:
    return msg["channel"]["alternatives"][0]["transcript"]


def sid_of(msg: dict) -> int:
    """request_id is "<front-end label>-<sid>"; the fake engine's tokens use the sid."""
    return int(msg["metadata"]["request_id"].rsplit("-", 1)[1])


def recv_until_final(ws: ClientConnection) -> list[dict]:
    messages = []
    while True:
        msg = json.loads(ws.recv(timeout=5))
        messages.append(msg)
        if msg["type"] == "Results" and msg["is_final"]:
            return messages


def recv_until_closed(ws: ClientConnection) -> tuple[list[dict], ConnectionClosed]:
    messages = []
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            messages.append(json.loads(ws.recv(timeout=5)))
    return messages, closed.value


def test_partials_then_final_then_a_fresh_utterance(server):
    with connect(server.listen_url()) as ws:
        send_audio(ws, 2.0)  # 3 full chunks + 0.32 s left over
        finalize(ws)
        messages = recv_until_final(ws)
        sid = sid_of(messages[0])
        assert [text(m) for m in messages if not m["is_final"]] == [
            f"s{sid}c0",
            f"s{sid}c0 s{sid}c1",
            f"s{sid}c0 s{sid}c1 s{sid}c2",
        ]
        final = messages[-1]
        assert text(final) == f"s{sid}c0 s{sid}c1 s{sid}c2 s{sid}c3"
        assert (final["start"], final["duration"]) == (0.0, 2.0)
        assert final["speech_final"] and final["from_finalize"]

        send_audio(ws, 0.6)  # the next utterance starts where the last ended
        msg = json.loads(ws.recv(timeout=5))
        assert (text(msg), msg["is_final"]) == (f"s{sid}c0", False)
        assert (msg["start"], msg["duration"], msg["utt"]) == (2.0, 0.56, 2)


def test_back_to_back_turns_keep_their_boundaries_and_seq(server):
    # Turn 2 and its Finalize arrive while turn 1's final may still be in flight.
    with connect(server.listen_url()) as ws:
        send_audio(ws, 0.3)
        finalize(ws, seq=1)
        send_audio(ws, 0.2)
        finalize(ws, seq=2)
        first = recv_until_final(ws)[-1]
        second = recv_until_final(ws)[-1]
        assert (first["seq"], first["start"], first["duration"]) == (1, 0.0, 0.3)
        assert (second["seq"], second["start"], second["duration"]) == (2, 0.3, 0.2)


def test_closestream_delivers_owed_finals_then_metadata_then_1000(server):
    # Even sent immediately after connecting: the call's last words aren't lost.
    with connect(server.listen_url()) as ws:
        send_audio(ws, 0.2)
        finalize(ws, seq=4)
        ws.send(json.dumps({"type": "CloseStream"}))
        messages, closed = recv_until_closed(ws)
        assert [m["type"] for m in messages] == ["Results", "Metadata"]
        assert messages[0]["seq"] == 4 and messages[0]["duration"] == 0.2
        assert messages[1]["duration"] == 0.2
        assert closed.rcvd is not None and closed.rcvd.code == 1000


def test_finalize_without_audio_returns_an_empty_final(server):
    with connect(server.listen_url()) as ws:
        finalize(ws)
        final = recv_until_final(ws)[-1]
        assert text(final) == "" and final["from_finalize"]


def test_many_sockets_each_get_only_their_own_text(server):
    errors: list[Exception] = []

    def call(i: int) -> None:
        try:
            with connect(server.listen_url(call_id=f"T{i}")) as ws:
                send_audio(ws, 1.2)
                finalize(ws)
                final = recv_until_final(ws)[-1]
                tokens = text(final).split()
                sid = sid_of(final)
                assert tokens and all(t.startswith(f"s{sid}c") for t in tokens), final
        except Exception as exc:  # collected so the main thread can fail the test
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []


@pytest.mark.parametrize(
    "params",
    [
        {"sample_rate": 7000},
        {"sample_rate": "8000.0"},
        {"encoding": "mulaw"},
        {"channels": 2},
        {"multichannel": "true"},
        {"call_id": "bad\nline"},
    ],
)
def test_a_bad_connect_url_closes_only_that_socket_with_1008(server, params):
    with connect(server.listen_url(call_id="good")) as good:
        with connect(server.listen_url(**params)) as bad:
            _, closed = recv_until_closed(bad)
            assert closed.rcvd is not None and closed.rcvd.code == 1008
            assert closed.rcvd.reason.startswith("bad_config")
        send_audio(good, 0.6)  # a healthy socket beside it is untouched
        assert json.loads(good.recv(timeout=5))["type"] == "Results"


def test_an_unknown_text_message_closes_the_socket_with_1008(server):
    with connect(server.listen_url()) as ws:
        ws.send('{"type":"Nope"}')
        _, closed = recv_until_closed(ws)
        assert closed.rcvd is not None and closed.rcvd.code == 1008


def test_a_silent_stream_is_closed_but_keepalive_keeps_it_open(server_factory):
    server = server_factory(STT_IDLE_TIMEOUT_S="1")
    with connect(server.listen_url()) as silent, connect(server.listen_url()) as kept:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            kept.send(json.dumps({"type": "KeepAlive"}))
            time.sleep(0.3)
        _, closed = recv_until_closed(silent)
        assert closed.rcvd is not None and closed.rcvd.code == 1011
        assert closed.rcvd.reason.startswith("idle")
        send_audio(kept, 0.6)  # still open and working
        assert json.loads(kept.recv(timeout=5))["type"] == "Results"


def test_oversized_message_is_refused(server):
    with connect(server.listen_url()) as ws:
        ws.send(b"\0" * (64 * 1024 + 2))
        _, closed = recv_until_closed(ws)
        assert closed.rcvd is not None and closed.rcvd.code == 1009


def test_a_client_far_faster_than_real_time_is_closed_alone(server):
    # A rate limit: ~1.5x real time plus a 5 s burst, per client.
    with connect(server.listen_url()) as good, connect(server.listen_url()) as fast:
        with pytest.raises(ConnectionClosed) as closed:
            for _ in range(80):  # 8 s of audio as fast as possible
                fast.send(b"\0" * FRAME)
            while True:
                fast.recv(timeout=5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1008
        assert closed.value.rcvd.reason.startswith("too_fast")
        send_audio(good, 0.6)
        assert json.loads(good.recv(timeout=5))["type"] == "Results"


def test_a_real_time_burst_within_the_allowance_is_fine(server):
    with connect(server.listen_url()) as ws:
        send_audio(ws, 3.0)  # inside the 5 s burst allowance
        finalize(ws)
        assert recv_until_final(ws)[-1]["duration"] == 3.0


def test_a_finalize_flood_closes_the_socket(server_factory):
    server = server_factory(STT_MAX_PENDING_FINALS="2")
    with connect(server.listen_url()) as ws:
        for _ in range(5):  # faster than any final can come back
            finalize(ws)
        _, closed = recv_until_closed(ws)
        assert closed.rcvd is not None and closed.rcvd.code == 1008
        assert closed.rcvd.reason.startswith("too_many_finalize")
