"""The model process on its own: started like the supervisor will start it,
driven over its real IPC connection like a front-end will drive it."""

import multiprocessing as mp
import os

import pytest
from stt import ipc
from stt.config import Settings
from stt.model import main as model_main

RATE = 8000
CHUNK = RATE * 560 // 1000 * 2


@pytest.fixture
def model():
    endpoint = ipc.make_endpoint()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(
        target=model_main.run,
        args=(Settings(), endpoint, ready, os.getpid()),
        daemon=True,
    )
    process.start()
    assert ready.wait(30), "model process never became ready"
    yield endpoint
    process.terminate()
    process.join(5)
    ipc.remove_endpoint(endpoint)


def _recv_until(conn, kind, timeout_s=5.0):
    """Collect result events until one of `kind` arrives."""
    seen = []
    while conn.poll(timeout_s):
        for event in conn.recv():
            seen.append(event)
            if event[0] == kind:
                return seen
    raise AssertionError(f"no {kind!r} within {timeout_s}s; got {seen}")


def test_a_call_over_ipc_gets_ready_partials_and_a_final(model):
    conn = ipc.connect(model)
    conn.send([("open", 1, {"sample_rate": RATE})])
    assert _recv_until(conn, ipc.OPENED)[-1] == (ipc.OPENED, 1, None)

    conn.send([("audio", 1, b"\0" * (CHUNK * 2 + 800))])
    conn.send([("finalize", 1, {"seq": 3})])
    events = _recv_until(conn, ipc.FINAL)
    partials = [e[2]["text"] for e in events if e[0] == ipc.PARTIAL]
    final = events[-1][2]
    assert partials == ["s1c0", "s1c0 s1c1"]
    assert (final["text"], final["seq"], final["utt"]) == ("s1c0 s1c1 s1c2", 3, 1)
    assert final["speech_end_s"] == 1.17
    conn.close()


def test_two_front_ends_get_only_their_own_results(model):
    a, b = ipc.connect(model), ipc.connect(model)
    for conn in (a, b):
        conn.send([("open", 5, {"sample_rate": RATE})])  # same sid on both
        _recv_until(conn, ipc.OPENED)
    a.send([("audio", 5, b"\0" * CHUNK)])
    assert _recv_until(a, ipc.PARTIAL)[-1][2]["text"] == "s5c0"
    assert not b.poll(0.3)  # nothing for the other front-end
    a.close()
    b.close()


def test_a_bad_peer_is_refused_and_good_ones_still_connect(model):
    bad = ipc.Endpoint(model.path, b"wrong key")
    with pytest.raises(Exception):
        ipc.connect(bad)
    good = ipc.connect(model)
    good.send([("open", 1, {"sample_rate": RATE})])
    assert _recv_until(good, ipc.OPENED)
    good.close()
