import threading
from multiprocessing import Pipe

from stt import ipc
from stt.model.ingest import run_ingest
from stt.model.streams import StreamTable


def test_shutdown_wakes_a_blocked_recv_without_closing_the_connection():
    # Review #1: only the owning thread may close(); shutdown() must not.
    ours, theirs = Pipe()
    result = []
    reader = threading.Thread(target=lambda: _recv_into(ours, result))
    reader.start()
    ipc.shutdown(ours)
    reader.join(5)
    assert result == ["EOF"]
    assert not ours.closed  # still owned (and closed) by its owner
    ours.close()
    theirs.close()


def _recv_into(conn, result):
    try:
        conn.recv()
        result.append("data")
    except (EOFError, OSError):
        result.append("EOF")


class RecordingWriter:
    def put(self, msg):
        self.sent.append(msg)

    def __init__(self, stops=True):
        self.sent = []
        self.stops = stops

    def shutdown(self):
        pass

    def join(self, timeout_s):
        return self.stops


class Writers:
    def remove(self, conn_id):
        pass


def test_bad_events_close_only_their_stream_and_never_kill_ingest():
    # Review #3/#5: a malformed event closes its own stream (no half-reset), and
    # even an unprintable/unsliceable event can't take the ingest thread down.
    table = StreamTable(chunk_ms=560, max_pending_s=5.0, max_utterance_s=30.0)
    writer = RecordingWriter()
    ours, theirs = Pipe()
    t = threading.Thread(
        target=run_ingest, args=(1, ours, table, writer, Writers()), daemon=True
    )
    t.start()
    theirs.send([("open", 1, {"sample_rate": 8000})])
    theirs.send([("open", 2, {"sample_rate": 8000})])
    theirs.send([None, 42, ("finalize", 1, 5), ("audio", 1, "not bytes")])
    theirs.send("not a list")
    theirs.send([("audio", 2, b"\0" * 8960)])  # the healthy stream still works
    theirs.close()  # front-end gone → ingest ends and closes its side
    t.join(5)
    assert not t.is_alive()
    errors = [m for m in writer.sent if m[0] == ipc.ERROR]
    assert {m[1] for m in errors} == {1}  # only stream 1; stream 2 untouched
    work = table.take_work()
    # Both streams were closed (by the error, then by EOF) before any feeder
    # cycle, so the engine is never touched for them (#9) and the rows are gone.
    assert work.opened == [] and work.released == [] and len(table) == 0
    assert ours.closed  # the owner closed it


def test_ingest_never_closes_a_connection_its_writer_may_still_use():
    # Review 2 #2: if the writer didn't stop in time, closing would race its send().
    table = StreamTable(chunk_ms=560, max_pending_s=5.0, max_utterance_s=30.0)
    ours, theirs = Pipe()
    t = threading.Thread(
        target=run_ingest,
        args=(1, ours, table, RecordingWriter(stops=False), Writers()),
        daemon=True,
    )
    t.start()
    theirs.close()
    t.join(5)
    assert not ours.closed
    ours.close()


def test_a_stalled_handshake_does_not_block_other_front_ends():
    # Review 2 #3: a peer that connects and never answers the challenge must not
    # stop a real front-end from connecting.
    import socket as socket_mod

    endpoint = ipc.make_endpoint()
    listener = ipc.listen(endpoint)
    accepted = []
    threading.Thread(
        target=ipc.serve,
        args=(listener, endpoint.authkey, accepted.append, 0.5),
        daemon=True,
    ).start()
    staller = socket_mod.socket(socket_mod.AF_UNIX)
    good = None
    try:
        staller.connect(endpoint.path)  # connects, then says nothing
        good = ipc.connect(endpoint)  # must complete despite the staller
        for _ in range(50):
            if accepted:
                break
            threading.Event().wait(0.05)
        assert len(accepted) == 1
    finally:
        if good is not None:
            good.close()
        staller.close()
        for conn in accepted:
            conn.close()
        listener.close()
        ipc.remove_endpoint(endpoint)


def test_a_bad_sample_rate_from_a_front_end_closes_only_that_stream():
    # Review 5 #5: 0 Hz would mean 0-byte chunks (a busy loop) and a divide by zero.
    table = StreamTable(chunk_ms=560, max_pending_s=5.0, max_utterance_s=30.0)
    writer = RecordingWriter()
    ours, theirs = Pipe()
    t = threading.Thread(
        target=run_ingest, args=(1, ours, table, writer, Writers()), daemon=True
    )
    t.start()
    theirs.send([("open", 1, {"sample_rate": 0}), ("open", 2, {"sample_rate": 8000})])
    theirs.close()
    t.join(5)
    assert [m[1] for m in writer.sent if m[0] == ipc.ERROR] == [1]


def test_an_unreadable_frame_ends_only_that_connection():
    # Review 5 #4: a frame that won't unpickle must not kill the model process.
    table = StreamTable(chunk_ms=560, max_pending_s=5.0, max_utterance_s=30.0)
    ours, theirs = Pipe()
    t = threading.Thread(
        target=run_ingest,
        args=(1, ours, table, RecordingWriter(), Writers()),
        daemon=True,
    )
    t.start()
    theirs.send([("open", 1, {"sample_rate": 8000})])
    theirs.send_bytes(b"this is not a pickle")
    t.join(5)
    assert not t.is_alive()  # the thread ended normally, not by an exception
    assert ours.closed
    theirs.close()


def test_an_idle_writer_stops_at_once_on_shutdown():
    # Review 8 #6: no polling, so shutdown must wake a writer idle in get().
    from stt.model.scheduler import ConnectionWriter

    ours, theirs = Pipe()
    writer = ConnectionWriter(1, ours, max_backlog=10)
    writer.start()
    writer.shutdown()
    assert writer.join(timeout_s=0.5)
    ours.close()
    theirs.close()
