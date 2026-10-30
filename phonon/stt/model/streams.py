"""The stream table: one row per live call, keyed by (conn_id, sid).

Written by ingest threads, read by the feeder. One lock guards rows and the
ready set; it is held only for bookkeeping — never during an engine step, never
around I/O or logging — so there is no lock ordering that could deadlock.

Turn boundaries are byte positions. Each `finalize` records where in `pending`
its turn ends; a final takes audio only up to that mark, and every finalize
gets its own mark (review #2, #3). Audio that arrives after a finalize belongs
to the next turn even if the feeder hasn't caught up yet.

Rows are removed in two steps: an ingest thread only marks a row closed; the
feeder removes it on its next cycle and releases its engine state, because only
the feeder may touch the engine.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from stt import ipc
from stt.model.engine import Key, StepKind, StepResult, WorkItem


@dataclass
class FinalMark:
    pos: int  # byte offset in `pending` where the turn ends
    seq: int | None  # the client's finalize seq, echoed on the final
    requested_at: float
    # Samples of the turn AFTER this mark that an overload drop already removed;
    # they count toward that turn's length once it becomes current (review #6).
    next_turn_dropped: int = 0


@dataclass
class Stream:
    key: Key
    sample_rate: int
    pending: bytearray = field(default_factory=bytearray)
    finals: deque[FinalMark] = field(default_factory=deque)
    in_utterance: bool = False
    closed: bool = False
    utt: int = 1  # current utterance number, sent on partials and finals
    utt_samples: int = 0  # samples taken in the current utterance
    # The stream clock: samples cut from `pending` in stream order, whether
    # stepped or dropped. A result reports the clock at the END OF ITS OWN
    # chunk (`ChunkReceipt.end`), so a drop that happens while a chunk is on the GPU
    # can't push that chunk's timestamp forward.
    clock: int = 0
    dropping: bool = False  # inside an overload episode (log/warn once per episode)


@dataclass(frozen=True)
class ChunkReceipt:
    """The receipt for one chunk handed to the engine: what apply() needs to
    report its result. Lives only for the cycle, so nothing per-cycle is left on
    the row if a step fails."""

    end: int  # stream clock at the end of this item's audio
    # The client finalize this final answers; None on a final means it was a
    # forced cut (max utterance length).
    mark: FinalMark | None


@dataclass
class FeederWork:
    """Everything the feeder must do in one cycle, cut under the lock at once."""

    opened: list[Key]
    released: list[Key]
    items: list[WorkItem]
    receipts: dict[Key, ChunkReceipt] = field(default_factory=dict)


Outgoing = tuple[int, ipc.Event]  # (conn_id, message)


class StreamTable:
    def __init__(
        self,
        chunk_ms: int,
        max_pending_s: float,
        max_utterance_s: float,
    ) -> None:
        self._chunk_ms = chunk_ms
        self._max_pending_s = max_pending_s
        self._max_utterance_s = max_utterance_s
        self._rows: dict[Key, Stream] = {}
        self._ready: set[Key] = set()
        self._opened: list[Key] = []
        self._closing: list[Key] = []
        self._lock = threading.Lock()
        self.wake = threading.Event()

    # ---- written by ingest threads -------------------------------------------

    def open(self, key: Key, sample_rate: int) -> bool:
        with self._lock:
            if key in self._rows:
                return False
            self._rows[key] = Stream(key, sample_rate)
            self._opened.append(key)
        self.wake.set()
        return True

    def append(self, key: Key, audio: bytes) -> int:
        """Add audio. Returns the bytes dropped if this append STARTED an overload
        episode (so the caller logs and warns once, outside the lock), else 0."""
        started_dropping = 0
        with self._lock:
            s = self._rows.get(key)
            if s is None or s.closed:
                return 0
            s.pending += audio
            cap = self._cap_bytes(s)
            if len(s.pending) > cap:
                drop = _even(len(s.pending) - cap)
                self._drop(s, drop)
                if not s.dropping:
                    s.dropping = True
                    started_dropping = drop
            if self._has_work(s):
                self._mark_ready(key)
        return started_dropping

    def request_final(
        self, key: Key, seq: int | None, received_at: float | None = None
    ) -> None:
        """received_at: when the front-end got the finalize (time.monotonic), so
        server_ms includes the front-end and IPC hop. The front-end limits how
        many finalizes may wait (max_pending_finals)."""
        with self._lock:
            s = self._rows.get(key)
            if s is None or s.closed:
                return
            stamp = received_at if received_at is not None else time.monotonic()
            s.finals.append(FinalMark(len(s.pending), seq, stamp))
            self._mark_ready(key)

    def mark_closed(self, key: Key) -> None:
        with self._lock:
            self._close_locked(key)
        self.wake.set()

    def mark_connection_closed(self, conn_id: int) -> int:
        with self._lock:
            keys = [k for k in self._rows if k[0] == conn_id]
            for key in keys:
                self._close_locked(key)
        self.wake.set()
        return len(keys)

    # ---- read by the feeder ----------------------------------------------------

    def take_work(self) -> FeederWork:
        """Cut the feeder's next batch of work. A stream appears at most once per cycle.

        The current turn is the audio before the first finalize mark (or all of
        pending if there is none). A full chunk of it steps normally; a shorter
        remainder ends the turn as a final. A turn that reaches the maximum
        utterance length is ended by a forced final (review #9).
        """
        with self._lock:
            opened, self._opened = self._opened, []
            released, self._closing = self._closing, []
            for key in released:
                self._rows.pop(key, None)
            # Opened and closed before the feeder ran: never touch the engine for
            # it, and never send `ready` for a socket that is already gone (#9).
            gone = set(opened) & set(released)
            if gone:
                opened = [k for k in opened if k not in gone]
                released = [k for k in released if k not in gone]
            items = []
            receipts: dict[Key, ChunkReceipt] = {}
            for key in list(self._ready):
                s = self._rows.get(key)
                if s is None:
                    self._ready.discard(key)
                    continue
                cut = self._take(s)
                if cut is not None:
                    items.append(cut[0])
                    receipts[key] = cut[1]
                if not self._has_work(s):
                    self._ready.discard(key)
                if s.dropping and len(s.pending) <= self._cap_bytes(s) // 2:
                    s.dropping = False
            if self._ready:
                # Streams still behind: run another cycle now, don't wait for
                # the next append to wake the feeder.
                self.wake.set()
            return FeederWork(opened, released, items, receipts)

    def apply(
        self, results: list[StepResult], receipts: dict[Key, ChunkReceipt]
    ) -> list[Outgoing]:
        """Fold step results into rows. A result for a closed row is dropped."""
        out: list[Outgoing] = []
        now = time.monotonic()
        with self._lock:
            for r in results:
                s = self._rows.get(r.key)
                t = receipts.get(r.key)
                if s is None or s.closed or t is None:
                    continue
                end_s = round(t.end / s.sample_rate, 3)
                conn_id, sid = r.key
                if r.kind.final:
                    mark = t.mark
                    payload = {
                        "text": r.text,  # the whole utterance text; never joined
                        "speech_end_s": end_s,
                        # Time from the client's finalize to this final; none
                        # for a forced cut, which no client asked for.
                        "server_ms": (
                            None
                            if mark is None
                            else int((now - mark.requested_at) * 1000)
                        ),
                        "utt": s.utt,
                        "seq": None if mark is None else mark.seq,
                        "forced": mark is None,
                    }
                    out.append((conn_id, (ipc.FINAL, sid, payload)))
                    s.utt += 1
                else:
                    payload = {"text": r.text, "audio_end_s": end_s, "utt": s.utt}
                    out.append((conn_id, (ipc.PARTIAL, sid, payload)))
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    # ---- internals (lock held) -------------------------------------------------

    def _take(self, s: Stream) -> tuple[WorkItem, ChunkReceipt] | None:
        chunk = self._chunk_bytes(s)
        mark = s.finals[0] if s.finals else None
        turn_bytes = mark.pos if mark is not None else len(s.pending)
        first = not s.in_utterance
        if mark is not None and turn_bytes <= chunk:
            # The rest of this turn fits in one step: end it now, even when it is
            # exactly one chunk (no extra empty-audio final cycle).
            s.finals.popleft()
            audio = self._cut(s, _even(mark.pos))
            final = True
            answers: FinalMark | None = mark
        elif turn_bytes >= chunk:
            audio = self._cut(s, chunk)
            s.utt_samples += chunk // 2
            max_samples = int(self._max_utterance_s * s.sample_rate)
            final = s.utt_samples >= max_samples
            answers = None
        else:
            return None
        if final:
            s.in_utterance = False
            # A turn that becomes current inherits the samples a drop took from it.
            s.utt_samples = (
                mark.next_turn_dropped if mark is not None and answers is mark else 0
            )
        else:
            s.in_utterance = True
        item = WorkItem(s.key, StepKind(first=first, final=final), audio, s.sample_rate)
        return item, ChunkReceipt(end=s.clock, mark=answers)

    def _cut(self, s: Stream, n: int) -> bytes:
        """Take n bytes from the front of pending; later marks move with the audio."""
        with memoryview(s.pending) as view:
            audio = bytes(view[:n])  # one copy; the view is released before del
        del s.pending[:n]
        s.clock += n // 2
        for mark in s.finals:
            mark.pos = max(0, mark.pos - n)
        return audio

    def _drop(self, s: Stream, n: int) -> None:
        """Overload: drop n bytes from the front, charging each turn only for its
        own lost audio. Marks inside the dropped region still produce their finals
        (with whatever audio is left), so a client waiting on a seq gets an answer."""
        marks = list(s.finals)
        current_end = marks[0].pos if marks else len(s.pending)
        s.utt_samples += min(n, current_end) // 2
        for i, mark in enumerate(marks):
            if mark.pos >= n:
                break
            next_end = marks[i + 1].pos if i + 1 < len(marks) else len(s.pending)
            mark.next_turn_dropped += (min(n, next_end) - mark.pos) // 2
        self._cut(s, n)

    def _has_work(self, s: Stream) -> bool:
        return bool(s.finals) or len(s.pending) >= self._chunk_bytes(s)

    def _mark_ready(self, key: Key) -> None:
        self._ready.add(key)
        self.wake.set()

    def _close_locked(self, key: Key) -> None:
        s = self._rows.get(key)
        if s is None or s.closed:
            return
        s.closed = True
        self._ready.discard(key)
        self._closing.append(key)

    def _chunk_bytes(self, s: Stream) -> int:
        return s.sample_rate * self._chunk_ms // 1000 * 2

    def _cap_bytes(self, s: Stream) -> int:
        return _even(int(s.sample_rate * self._max_pending_s) * 2)


def _even(n: int) -> int:
    """Whole PCM16 samples only: an odd byte stays for the next append."""
    return n - (n % 2)
