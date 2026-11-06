from stt import ipc
from stt.model.engine import StepKind, StepResult
from stt.model.streams import ChunkReceipt, FeederWork, Outgoing, StreamTable

RATE = 8000
CHUNK = RATE * 560 // 1000 * 2  # 8960 bytes
MS100 = RATE // 10 * 2  # 1600 bytes
LATER = StepKind(first=False, final=False)


class _Table(StreamTable):
    """Test helper: apply() uses the most recent cycle's `receipts` by default, as
    the feeder does, so tests read as take → apply."""

    last: dict[tuple[int, int], ChunkReceipt] = {}

    def take_work(self) -> FeederWork:
        work = super().take_work()
        self.last = work.receipts
        return work

    def apply(
        self,
        results: list[StepResult],
        receipts: dict[tuple[int, int], ChunkReceipt] | None = None,
    ) -> list[Outgoing]:
        return super().apply(results, self.last if receipts is None else receipts)


def _table(max_pending_s: float = 5.0, max_utterance_s: float = 30.0) -> _Table:
    return _Table(560, max_pending_s, max_utterance_s)


def _open(t: _Table, key: tuple[int, int] = (1, 1)) -> tuple[int, int]:
    t.open(key, RATE)
    t.take_work()  # consume the `opened` notice
    return key


def test_ready_only_when_a_full_chunk_is_there():
    t = _table()
    key = _open(t)
    t.append(key, b"\0" * (CHUNK - 2))
    assert t.take_work().items == []
    t.append(key, b"\0\0")
    items = t.take_work().items
    assert len(items) == 1 and len(items[0].audio) == CHUNK
    assert items[0].kind == StepKind(first=True, final=False)


def test_second_chunk_is_a_later_step():
    t = _table()
    key = _open(t)
    t.append(key, b"\0" * CHUNK * 2)
    assert t.take_work().items[0].kind.first
    assert not t.take_work().items[0].kind.first


def test_odd_byte_waits_for_the_next_append():
    t = _table()
    key = _open(t)
    t.append(key, b"\1" * 1001)
    t.request_final(key, None)
    item = t.take_work().items[0]
    assert len(item.audio) == 1000 and item.kind.final


def test_final_takes_only_audio_from_before_its_finalize():
    # Review #2: 100 ms of turn 1, finalize, 200 ms of turn 2 — all before the
    # feeder runs. The final must carry 100 ms, not 300 ms.
    t = _table()
    key = _open(t)
    t.append(key, b"\1" * MS100)
    t.request_final(key, seq=1)
    t.append(key, b"\2" * MS100 * 2)
    item = t.take_work().items[0]
    assert item.kind.final
    assert item.audio == b"\1" * MS100
    # Turn 2 is untouched and starts a fresh utterance.
    t.append(key, b"\2" * (CHUNK - MS100 * 2))
    nxt = t.take_work().items[0]
    assert nxt.kind == StepKind(first=True, final=False)
    assert nxt.audio == b"\2" * CHUNK


def test_a_finalize_during_a_final_in_flight_is_not_lost():
    # Review #3: final #1 is being stepped; a short turn and its finalize arrive.
    t = _table()
    key = _open(t)
    t.append(key, b"\1" * MS100)
    t.request_final(key, seq=1)
    cycle1 = t.take_work()
    first = cycle1.items[0]  # final #1 now "on the GPU"
    t.append(key, b"\2" * MS100)
    t.request_final(key, seq=2)
    out1 = t.apply([StepResult(key, first.kind, "turn one")], cycle1.receipts)
    second = t.take_work().items
    assert len(second) == 1 and second[0].kind.final
    assert second[0].audio == b"\2" * MS100
    out2 = t.apply([StepResult(key, second[0].kind, "turn two")])
    assert out1[0][1][2] | {"server_ms": 0} == {
        "text": "turn one",
        "speech_end_s": 0.1,
        "server_ms": 0,
        "utt": 1,
        "seq": 1,
        "forced": False,
    }
    assert (out2[0][1][2]["utt"], out2[0][1][2]["seq"]) == (2, 2)
    assert out2[0][1][2]["speech_end_s"] == 0.2


def test_finalize_with_no_audio_still_produces_a_final():
    t = _table()
    key = _open(t)
    t.request_final(key, None)
    item = t.take_work().items[0]
    assert item.kind == StepKind(first=True, final=True) and item.audio == b""
    out = t.apply([StepResult(key, item.kind, "")])
    assert out[0][1][0] == ipc.FINAL and out[0][1][2]["text"] == ""


def test_final_with_more_than_a_chunk_steps_normally_first():
    t = _table()
    key = _open(t)
    t.append(key, b"\0" * (CHUNK + 100))
    t.request_final(key, None)
    a = t.take_work().items[0]
    b = t.take_work().items[0]
    assert not a.kind.final and len(a.audio) == CHUNK
    assert b.kind.final and len(b.audio) == 100


def test_a_turn_without_finalize_is_cut_at_the_max_length():
    # Review #9: no finalize ever arrives; the utterance must still end.
    t = _table(max_utterance_s=1.12)  # exactly two chunks
    key = _open(t)
    t.append(key, b"\0" * CHUNK * 3)
    kinds = [t.take_work().items[0].kind for _ in range(3)]
    assert kinds == [
        StepKind(first=True, final=False),
        StepKind(first=False, final=True),
        StepKind(first=True, final=False),
    ]


def test_forced_final_is_marked():
    t = _table(max_utterance_s=0.56)
    key = _open(t)
    t.append(key, b"\0" * CHUNK)
    item = t.take_work().items[0]
    assert item.kind.final
    out = t.apply([StepResult(key, item.kind, "x")])
    assert out[0][1][2]["forced"] is True and out[0][1][2]["seq"] is None


def test_text_is_replaced_not_joined():
    # Review #13: RNNT pieces continue a word; the engine sends the whole text.
    t = _table()
    key = _open(t)
    t.append(key, b"\0" * CHUNK * 2)
    first = t.take_work().items[0]
    t.apply([StepResult(key, first.kind, "kar")])
    second = t.take_work().items[0]
    out = t.apply([StepResult(key, second.kind, "karta")])
    assert out[0][1] == (
        ipc.PARTIAL,
        1,
        {"text": "karta", "audio_end_s": 1.12, "utt": 1},
    )


def test_overload_drop_moves_marks_counts_time_and_reports_once():
    # Review #7: dropped audio is counted on the client's clock, the finalize
    # mark moves with it, and only the first drop of an episode is reported.
    t = _table(max_pending_s=1.0)
    key = _open(t)
    t.append(key, b"\1" * RATE * 2)  # exactly at the 1 s cap: no drop
    t.request_final(key, seq=7)
    first = t.append(key, b"\2" * MS100)
    again = t.append(key, b"\2" * MS100)
    assert first == MS100 and again == 0
    items = t.take_work().items  # turn 1 is now 0.8 s, so a normal chunk first
    assert len(items[0].audio) == CHUNK and not items[0].kind.final
    t.apply([StepResult(key, items[0].kind, "a")])
    fin = t.take_work().items[0]
    assert fin.kind.final and len(fin.audio) == RATE * 2 - 2 * MS100 - CHUNK
    out = t.apply([StepResult(key, fin.kind, "a b")])
    # 0.2 s dropped + 0.8 s stepped = the full 1.0 s the client sent for turn 1.
    assert out[0][1][2]["speech_end_s"] == 1.0


def test_two_step_removal_reports_opened_and_released():
    t = _table()
    key = (2, 7)
    t.open(key, RATE)
    assert t.take_work().opened == [key]
    t.append(key, b"\0" * CHUNK)
    t.mark_closed(key)
    work = t.take_work()
    assert work.released == [key] and work.items == []
    assert len(t) == 0


def test_result_for_a_closed_stream_is_dropped():
    t = _table()
    key = _open(t)
    t.mark_closed(key)
    assert t.apply([StepResult(key, LATER, "hi")]) == []


def test_connection_drop_closes_only_its_streams():
    t = _table()
    t.open((1, 1), RATE)
    t.open((1, 2), RATE)
    t.open((2, 1), RATE)
    t.take_work()
    assert t.mark_connection_closed(1) == 2
    assert sorted(t.take_work().released) == [(1, 1), (1, 2)]
    assert len(t) == 1


def test_open_and_close_in_the_same_cycle_never_touch_the_engine():
    # Review #9: no engine.open/release and no `ready` for an already-gone socket.
    t = _table()
    t.open((1, 1), RATE)
    t.mark_closed((1, 1))
    work = t.take_work()
    assert work.opened == [] and work.released == [] and len(t) == 0


def test_a_drop_across_a_finalize_charges_each_turn_its_own_audio():
    # Review #6: turn 1 (0.5 s) and 0.3 s of turn 2 are dropped. Turn 1 still
    # gets its final; turn 2 starts already 0.3 s long, so with a 0.86 s limit
    # its first full chunk (0.56 s) is forced final. Charging the whole drop to
    # turn 1 (old code) would let turn 2 run 0.3 s too long.
    t = _table(max_pending_s=1.0, max_utterance_s=0.86)
    key = _open(t)
    t.append(key, b"\1" * (RATE // 2 * 2))  # 0.5 s, turn 1
    t.request_final(key, seq=1)
    t.append(key, b"\2" * (RATE // 2 * 2))  # 0.5 s of turn 2: at the cap
    assert t.append(key, b"\2" * (RATE * 8 // 10 * 2)) > 0  # +0.8 s → drop 0.8 s
    fin = t.take_work().items[0]
    assert fin.kind.final and fin.audio == b""  # turn 1's audio was all dropped
    t.apply([StepResult(key, fin.kind, "")])
    nxt = t.take_work().items[0]  # turn 2's first chunk
    assert nxt.kind == StepKind(first=True, final=True)


def test_a_drop_during_a_step_does_not_move_that_steps_timestamp():
    # Review 3 #3: the chunk on the GPU came BEFORE the dropped audio, so its
    # partial must say 0.56 s, not 0.56 s + the drop.
    t = _table(max_pending_s=1.0)
    key = _open(t)
    t.append(key, b"\0" * CHUNK)
    item = t.take_work().items[0]  # chunk 1 now "on the GPU"
    t.append(key, b"\0" * RATE * 2 * 3)  # 3 s arrives → 2 s dropped meanwhile
    out = t.apply([StepResult(key, item.kind, "a")])
    assert out[0][1][2]["audio_end_s"] == 0.56


def test_a_finalize_exactly_at_a_chunk_boundary_is_one_final_step():
    # Review 4 #9: no extra empty-audio final cycle.
    t = _table()
    key = _open(t)
    t.append(key, b"\0" * CHUNK)
    t.request_final(key, seq=1)
    item = t.take_work().items[0]
    assert item.kind == StepKind(first=True, final=True)
    assert len(item.audio) == CHUNK
    assert t.take_work().items == []
