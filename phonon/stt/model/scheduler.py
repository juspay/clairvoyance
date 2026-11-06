"""The feeder loop and the per-connection writers.

THE ONE RULE: the feeder thread does nothing but feed the engine. It never
waits on I/O — results go into a writer's queue, and that writer's thread is
the only code that sends on its connection.
"""

import contextlib
import queue
import threading
from collections import defaultdict
from multiprocessing.connection import Connection
from typing import Callable, Protocol

from loguru import logger
from stt import ipc, proc
from stt.model.engine import (
    Engine,
    FatalEngineError,
    Key,
    StepKind,
    StepResult,
    WorkItem,
)
from stt.model.streams import Outgoing, StreamTable

_STOP: object = object()  # wakes an idle writer so it can exit


class ConnectionWriter:
    """The only sender on one front-end connection.

    If the front-end stops reading and the backlog passes its limit, the
    connection is shut down: that front-end's calls reconnect elsewhere, and the
    feeder — every other call on this GPU — never waits for it. The ingest thread
    owns the connection and is the one that finally closes it.
    """

    def __init__(self, conn_id: int, conn: Connection, max_backlog: int) -> None:
        self.conn_id = conn_id
        self._conn = conn
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_backlog)
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"writer-{conn_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def put(self, msg: ipc.Event) -> None:
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            logger.error(
                f"conn {self.conn_id}: outbound backlog full; closing the connection"
            )
            self.shutdown()

    def shutdown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        ipc.shutdown(self._conn)
        # Wake the writer if it is idle in get(). If the queue is full, it isn't
        # idle: its next send() fails on the shut socket and it exits.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(_STOP)

    def join(self, timeout_s: float) -> bool:
        """Wait for the writer thread to stop. True if it did."""
        if self._thread.is_alive():
            self._thread.join(timeout_s)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            first = self._queue.get()  # no polling: shutdown() wakes us
            if first is _STOP or self._closed.is_set():
                return
            batch = [first]
            while True:
                try:
                    msg = self._queue.get_nowait()
                except queue.Empty:
                    break
                if msg is _STOP:
                    return
                batch.append(msg)
            try:
                self._conn.send(batch)
            except (OSError, EOFError, ValueError):
                self.shutdown()
                return


class WriterRegistry:
    """conn_id → writer. Added by the accept loop, removed by ingest threads."""

    def __init__(self) -> None:
        self._writers: dict[int, ConnectionWriter] = {}
        self._lock = threading.Lock()

    def add(self, writer: ConnectionWriter) -> None:
        with self._lock:
            self._writers[writer.conn_id] = writer

    def remove(self, conn_id: int) -> None:
        with self._lock:
            self._writers.pop(conn_id, None)

    def get(self, conn_id: int) -> ConnectionWriter | None:
        with self._lock:
            return self._writers.get(conn_id)


class OutboundWriter(Protocol):
    def put(self, msg: ipc.Event) -> None: ...


class WriterLookup(Protocol):
    def get(self, conn_id: int) -> OutboundWriter | None: ...


class Feeder:
    # Every table change sets `wake`; this is only a safety net, not a poll (#7).
    IDLE_WAIT_S = 1.0

    def __init__(
        self,
        table: StreamTable,
        engine: Engine,
        writers: WriterLookup,
        exit_process: Callable[[int], None] = proc.exit_now,
    ) -> None:
        self._table = table
        self._engine = engine
        self._writers = writers
        self._exit_process = exit_process
        # Keys whose engine.open failed: closing them must not call release.
        self._never_opened: set[Key] = set()

    def run(self) -> None:
        while True:
            self._table.wake.wait(self.IDLE_WAIT_S)
            self.run_once()

    def run_once(self) -> None:
        # Clear BEFORE taking: a wake-up set during the take is kept.
        self._table.wake.clear()
        work = self._table.take_work()
        out: list[Outgoing] = []
        try:
            failed = self._open(work.opened, out)
            # `ready` (and open errors) go out now, not after the whole cycle's
            # step: a new call shouldn't wait for everyone else's GPU step.
            self._deliver(out)
            out = []
            self._release(work.released)
            items = [i for i in work.items if i.key not in failed]
            out.extend(self._table.apply(self._step(items, out), work.receipts))
        except FatalEngineError:
            # The engine is broken for every stream; stepping on would fail call
            # after call (review #1). Exit so the pod is restarted.
            logger.opt(exception=True).critical("fatal engine error; exiting")
            self._exit_process(1)
            return
        self._deliver(out)

    def _deliver(self, out: list[Outgoing]) -> None:
        for conn_id, msg in out:
            writer = self._writers.get(conn_id)
            if writer is not None:
                writer.put(msg)

    def _open(self, keys: list[Key], out: list[Outgoing]) -> set[Key]:
        """`ready` goes to the client only after the engine has opened the stream
        (review #14)."""
        failed: set[Key] = set()
        for key in keys:
            try:
                self._engine.open(key)
            except FatalEngineError:
                raise
            except Exception:
                logger.exception(f"engine.open failed for {key}")
                failed.add(key)
                self._never_opened.add(key)
                self._table.mark_closed(key)
                out.append(self._error(key, "engine_open"))
                continue
            out.append((key[0], (ipc.OPENED, key[1], None)))
        return failed

    def _release(self, keys: list[Key]) -> None:
        for key in keys:
            if key in self._never_opened:
                self._never_opened.discard(key)
                continue
            try:
                self._engine.release(key)
            except FatalEngineError:
                raise
            except Exception:
                logger.exception(f"engine.release failed for {key}")

    def _step(self, items: list[WorkItem], out: list[Outgoing]) -> list[StepResult]:
        groups: dict[StepKind, list[WorkItem]] = defaultdict(list)
        for item in items:
            groups[item.kind].append(item)
        results: list[StepResult] = []
        for kind, group in groups.items():
            try:
                results.extend(self._engine.step(kind, group))
            except FatalEngineError:
                raise
            except Exception:
                # Contain to this group: its streams are closed (engine state
                # released next cycle); the client reconnects and replays.
                logger.exception(
                    f"engine.step failed for {len(group)} streams ({kind})"
                )
                for item in group:
                    self._table.mark_closed(item.key)
                    out.append(self._error(item.key, "engine_step"))
        return results

    @staticmethod
    def _error(key: Key, code: str) -> Outgoing:
        conn_id, sid = key
        return conn_id, (
            ipc.ERROR,
            sid,
            {"code": code, "message": "transcription failed"},
        )
