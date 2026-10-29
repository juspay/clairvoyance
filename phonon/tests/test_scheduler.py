import pytest
from stt import ipc
from stt.model.engine import FatalEngineError, StepKind
from stt.model.fake_engine import FakeEngine
from stt.model.scheduler import Feeder
from stt.model.streams import StreamTable

RATE = 8000
CHUNK = RATE * 560 // 1000 * 2


class RecordingWriter:
    def __init__(self, conn_id):
        self.conn_id = conn_id
        self.sent = []

    def put(self, msg):
        self.sent.append(msg)


class Writers:
    def __init__(self, *conn_ids):
        self.by_id = {c: RecordingWriter(c) for c in conn_ids}

    def get(self, conn_id):
        return self.by_id.get(conn_id)


class Exited(Exception):
    pass


def _exit(code):
    raise Exited(code)


def _setup(*conn_ids, engine=None):
    table = StreamTable(chunk_ms=560, max_pending_s=5.0, max_utterance_s=30.0)
    engine = engine or FakeEngine()
    writers = Writers(*conn_ids)
    return table, engine, writers, Feeder(table, engine, writers, exit_process=_exit)


def _texts(writer, kind):
    return [m[2] for m in writer.sent if m[0] == kind]


def test_ready_is_sent_only_after_the_engine_opened_the_stream():
    # Review #14.
    order = []

    class Spy(FakeEngine):
        def open(self, key):
            order.append("engine.open")
            super().open(key)

    table, engine, writers, feeder = _setup(1, engine=Spy())
    table.open((1, 1), RATE)
    assert writers.by_id[1].sent == []
    feeder.run_once()
    assert order == ["engine.open"]
    assert writers.by_id[1].sent == [(ipc.OPENED, 1, None)]


def test_failed_open_sends_error_not_ready():
    class NoOpen(FakeEngine):
        def open(self, key):
            raise RuntimeError("no slot")

    table, engine, writers, feeder = _setup(1, engine=NoOpen())
    table.open((1, 1), RATE)
    feeder.run_once()
    assert [m[0] for m in writers.by_id[1].sent] == [ipc.ERROR]
    feeder.run_once()
    assert len(table) == 0  # the stream was closed and purged


def test_first_and_later_steps_never_share_a_batch():
    seen = []

    class Spy(FakeEngine):
        def step(self, kind, items):
            seen.append((kind, sorted(i.key for i in items)))
            return super().step(kind, items)

    table, engine, writers, feeder = _setup(1, engine=Spy())
    table.open((1, 1), RATE)
    table.open((1, 2), RATE)
    table.append((1, 1), b"\0" * CHUNK * 2)
    feeder.run_once()  # (1,1) first chunk
    table.append((1, 2), b"\0" * CHUNK)
    feeder.run_once()  # (1,1) later + (1,2) first: two separate calls
    assert sorted(seen[-2:], key=lambda s: s[0].first) == [
        (StepKind(first=False, final=False), [(1, 1)]),
        (StepKind(first=True, final=False), [(1, 2)]),
    ]


def test_results_go_to_the_owning_connection_only():
    table, engine, writers, feeder = _setup(1, 2)
    table.open((1, 5), RATE)
    table.open((2, 5), RATE)  # same sid on another front-end
    feeder.run_once()
    table.append((1, 5), b"\0" * CHUNK)
    table.append((2, 5), b"\0" * CHUNK)
    feeder.run_once()
    expected = {"text": "s5c0", "audio_end_s": 0.56, "utt": 1}
    assert _texts(writers.by_id[1], ipc.PARTIAL) == [expected]
    assert _texts(writers.by_id[2], ipc.PARTIAL) == [expected]


def test_a_failing_group_closes_only_its_streams():
    # Review #2/#4: a failed step closes its streams (engine state released),
    # it does not half-reset them; other streams keep going.
    class Flaky(FakeEngine):
        def step(self, kind, items):
            if any(i.key == (1, 1) for i in items):
                raise RuntimeError("boom")
            return super().step(kind, items)

    table, engine, writers, feeder = _setup(1, engine=Flaky())
    table.open((1, 1), RATE)
    table.open((1, 2), RATE)
    feeder.run_once()
    table.append((1, 1), b"\0" * CHUNK)
    feeder.run_once()  # (1,1) fails → ERROR, stream closed
    assert [m[:2] for m in writers.by_id[1].sent if m[0] == ipc.ERROR] == [
        (ipc.ERROR, 1)
    ]
    feeder.run_once()  # closed stream purged, engine state released
    assert engine.open_streams == 1 and len(table) == 1
    table.append((1, 2), b"\0" * CHUNK)
    feeder.run_once()  # the other stream is unaffected
    assert _texts(writers.by_id[1], ipc.PARTIAL)[-1]["text"] == "s2c0"


def test_a_fatal_engine_error_exits_the_process():
    # Review #1: a broken GPU must not keep failing call after call.
    class Broken(FakeEngine):
        def step(self, kind, items):
            raise FatalEngineError("CUDA error: an illegal memory access")

    table, engine, writers, feeder = _setup(1, engine=Broken())
    table.open((1, 1), RATE)
    feeder.run_once()
    table.append((1, 1), b"\0" * CHUNK)
    with pytest.raises(Exited) as exited:
        feeder.run_once()
    assert exited.value.args == (1,)


def test_closed_mid_step_drops_the_result_and_releases_state():
    table, engine, writers, feeder = _setup(1)

    class ClosesDuringStep(FakeEngine):
        def step(self, kind, items):
            out = super().step(kind, items)
            table.mark_closed((1, 1))  # the client hung up while we computed
            return out

    feeder._engine = engine = ClosesDuringStep()
    table.open((1, 1), RATE)
    feeder.run_once()
    table.append((1, 1), b"\0" * CHUNK)
    feeder.run_once()
    assert _texts(writers.by_id[1], ipc.PARTIAL) == []
    feeder.run_once()
    assert engine.open_streams == 0


def test_every_close_path_releases_engine_state():
    table, engine, writers, feeder = _setup(1, 2)
    for sid in (1, 2, 3):
        table.open((1, sid), RATE)
    table.open((2, 1), RATE)
    feeder.run_once()
    assert engine.open_streams == 4
    table.mark_closed((1, 1))  # normal close
    table.mark_connection_closed(2)  # front-end died
    feeder.run_once()
    assert engine.open_streams == 2


def test_final_ends_the_utterance_and_the_next_audio_starts_fresh():
    table, engine, writers, feeder = _setup(1)
    table.open((1, 3), RATE)
    feeder.run_once()
    table.append((1, 3), b"\0" * (CHUNK + 400))
    table.request_final((1, 3), seq=9)
    feeder.run_once()
    feeder.run_once()
    final = _texts(writers.by_id[1], ipc.FINAL)[-1]
    assert (final["text"], final["utt"], final["seq"]) == ("s3c0 s3c1", 1, 9)
    table.append((1, 3), b"\0" * CHUNK)
    feeder.run_once()
    # 4480 + 200 + 4480 samples stepped so far at 8 kHz
    assert _texts(writers.by_id[1], ipc.PARTIAL)[-1] == {
        "text": "s3c0",
        "audio_end_s": 1.145,
        "utt": 2,
    }


def test_the_real_feeder_thread_drains_a_burst_without_waiting():
    # Review 2 #1: with run_once() driven by hand this can't be seen. A burst of
    # three chunks plus a final must drain in one quick series of rounds, not
    # one chunk per idle timeout.
    import threading
    import time

    table, engine, writers, feeder = _setup(1)
    threading.Thread(target=feeder.run, daemon=True).start()
    table.open((1, 1), RATE)
    table.append((1, 1), b"\0" * (CHUNK * 3 + 400))
    table.request_final((1, 1), seq=1)
    started = time.monotonic()
    while time.monotonic() - started < 0.5:
        if _texts(writers.by_id[1], ipc.FINAL):
            break
        time.sleep(0.005)
    assert _texts(writers.by_id[1], ipc.FINAL), "final not out within 0.5 s"
    assert feeder.IDLE_WAIT_S >= 1.0  # so this test would fail without the re-wake


def test_a_stream_whose_open_failed_is_never_released():
    # Review 5 #6: the real engine may raise on releasing state it never made.
    released = []

    class NoOpen(FakeEngine):
        def open(self, key):
            raise RuntimeError("no slot")

        def release(self, key):
            released.append(key)

    table, engine, writers, feeder = _setup(1, engine=NoOpen())
    table.open((1, 1), RATE)
    feeder.run_once()  # open fails → stream closed
    feeder.run_once()  # purge cycle
    assert released == [] and len(table) == 0


def test_ready_is_sent_before_the_rounds_step():
    # Review 7 #6: a new call's `ready` must not wait for everyone else's step.
    order = []

    class SlowStep(FakeEngine):
        def step(self, kind, items):
            order.append("step")
            return super().step(kind, items)

    table, engine, writers, feeder = _setup(1, engine=SlowStep())
    table.open((1, 1), RATE)
    feeder.run_once()
    table.append((1, 1), b"\0" * CHUNK)  # (1,1) has a step this cycle …
    table.open((1, 2), RATE)  # … and (1,2) opens in the same cycle

    class Recorder(RecordingWriter):
        def put(self, msg):
            order.append(msg[0])
            super().put(msg)

    writers.by_id[1] = Recorder(1)
    feeder.run_once()
    assert order.index(ipc.OPENED) < order.index("step")
