"""A front-end's side of IPC.

    event loop ── enqueue() ──► pending ── flush thread (every FLUSH_MS) ──► model
    event loop ◄── call_soon_threadsafe ◄── receiver thread ◄────────────── model

The event loop never calls send() or recv() itself: a slow model must not
freeze this front-end's sockets (design review #3). The receiver thread never
touches a socket; it only hands batches to the loop.
"""

import asyncio
import threading
import time
from multiprocessing.connection import Connection
from typing import Any, Callable

from fastapi import WebSocket
from loguru import logger
from stt import ipc, proc
from stt.config import Settings
from stt.frontend.session import Session


def coalesce(events: list[ipc.Event]) -> list[ipc.Event]:
    """Merge audio events per stream to send fewer, larger pieces.

    Audio for one sid may only be merged across other sids' events, never across
    that sid's own finalize or close: `[audio, finalize, audio]` must keep its
    boundary, or the next utterance's first word lands in the previous final.
    """
    out: list[Any] = []
    open_audio: dict[int, int] = {}  # sid → index in `out` of its mergeable audio
    for kind, sid, payload in events:
        if kind == ipc.AUDIO:
            idx = open_audio.get(sid)
            if idx is None:
                open_audio[sid] = len(out)
                out.append([kind, sid, [payload]])
            else:
                out[idx][2].append(payload)
        else:
            # finalize / close end the run: later audio must not jump ahead of it
            open_audio.pop(sid, None)
            out.append([kind, sid, payload])
    # One copy per merged run (join), not one per piece.
    return [(k, s, b"".join(p) if k == ipc.AUDIO else p) for k, s, p in out]


class ModelLink:
    # Longer than Session.SENDER_DRAIN_S, so 1011 close frames reach clients
    # (with any backlog ahead of them) before the process exits.
    EXIT_GRACE_S = 2.5

    def __init__(self, conn: Connection, settings: Settings, label: str) -> None:
        self._conn = conn
        # Prefix for client-visible stream ids, unique within the pod and across
        # front-end restarts (e.g. "fe1-4242"); the sid stays a plain int inside.
        self.label = label
        self._settings = settings
        self._sessions: dict[int, Session] = {}
        self._next_sid = 0
        self._pending: list[ipc.Event] = []
        self._pending_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lost = threading.Event()
        self._lost_lock = threading.Lock()
        self._has_pending = threading.Event()  # set by enqueue(); no idle wakeups
        self._urgent = threading.Event()  # a non-audio event is waiting
        self._pending_bytes = 0

    # ---- event loop side -----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        threading.Thread(target=self._flush_loop, name="flush", daemon=True).start()
        threading.Thread(target=self._recv_loop, name="receiver", daemon=True).start()

    def open_session(self, ws: WebSocket) -> Session:
        # Never reused within this process, so a late result can't reach a new socket.
        self._next_sid += 1
        session = Session(ws, self._next_sid, self, self._settings)
        self._sessions[session.sid] = session
        return session

    def forget(self, sid: int) -> None:
        self._sessions.pop(sid, None)

    def enqueue(self, event: ipc.Event) -> None:
        """Queue an event for the model. Per-client speed is limited by each
        Session (a rate limit); this buffer's cap only catches a model that has
        stopped reading altogether."""
        with self._pending_lock:
            if event[0] == ipc.AUDIO:
                self._pending_bytes += len(event[2])
            else:
                self._urgent.set()  # finalize/close/open: don't make it wait
            self._pending.append(event)
            over = self._pending_bytes > self._settings.frontend_pending_max_bytes
        self._has_pending.set()
        if over and not self._lost.is_set():
            # The model has stopped reading (send() is blocked): it is as good as
            # dead to us. Fail fast instead of buffering until we are OOM-killed.
            logger.error("model is not reading; outbound buffer full")
            self._lose_model()

    @property
    def lost(self) -> bool:
        """The model process is gone; this front-end is about to exit."""
        return self._lost.is_set()

    def _dispatch(self, batch: list[ipc.Event]) -> None:
        for kind, sid, payload in batch:
            session = self._sessions.get(sid)
            if session is not None:
                session.on_result(kind, payload)

    def _on_model_lost(self) -> None:
        """The model process is gone: fail fast so callers reconnect elsewhere."""
        logger.error(
            f"model connection lost; closing {len(self._sessions)} sockets with 1011"
        )
        for session in list(self._sessions.values()):
            session.fail(
                1011, "server_error", "transcription is unavailable on this server"
            )
        assert self._loop is not None
        self._loop.call_later(self.EXIT_GRACE_S, proc.exit_now, proc.EXIT_MODEL_LOST)

    def _post(self, fn: Callable[..., object], *args: object) -> bool:
        """Run fn on the event loop. False if the loop is gone (uvicorn shutting
        down): late results are then dropped instead of crashing the thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # closed between the check and the call
            return False
        return True

    # ---- threads -------------------------------------------------------------------

    def _flush_loop(self) -> None:
        interval = self._settings.flush_ms / 1000
        while not self._lost.is_set():
            # Sleep until something is queued, then wait one interval so audio
            # batches up, unless a finalize/close/open is waiting: those go now.
            if not self._has_pending.wait(timeout=1.0):
                continue
            self._urgent.wait(interval)
            self._has_pending.clear()
            self._urgent.clear()
            with self._pending_lock:
                events, self._pending = self._pending, []
                self._pending_bytes = 0
            if not events:
                continue
            try:
                self._conn.send(coalesce(events))
            except (OSError, EOFError, ValueError):
                self._lose_model()
                return

    def _recv_loop(self) -> None:
        while True:
            try:
                batch = self._conn.recv()
            except Exception:  # EOF, OSError, or a frame that won't unpickle
                self._lose_model()
                return
            if not self._post(self._dispatch, batch):
                return  # shutting down; nothing left to deliver to

    def _lose_model(self) -> None:
        with self._lost_lock:  # called from three threads: act exactly once
            if self._lost.is_set():
                return
            self._lost.set()
        if not self._post(self._on_model_lost):
            proc.exit_now(proc.EXIT_MODEL_LOST)
