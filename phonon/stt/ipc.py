"""IPC between front-ends and the model process.

The model process listens on a Unix socket; each front-end connects to it and
gets its own connection. A dead process closes its connection, which the other
side sees as EOF — that is the failure signal, and it cannot get lost.

Messages are batches (lists) of events. Client data travels only as validated
fields and raw bytes, never as client-built objects (design review #16).

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
"""

import contextlib
import errno
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import (
    Client,
    Connection,
    Listener,
    answer_challenge,
    deliver_challenge,
)
from typing import Any, Callable

from loguru import logger

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

_TRANSIENT_ACCEPT_ERRORS = {
    errno.EMFILE,
    errno.ENFILE,
    errno.ENOBUFS,
    errno.ENOMEM,
    errno.ECONNABORTED,
    errno.EINTR,
}


@dataclass(frozen=True)
class Endpoint:
    path: str
    authkey: bytes


def make_endpoint() -> Endpoint:
    """A private 0700 directory per run, so runs never share a socket (#10).

    /tmp keeps the path short: Unix socket paths are limited to ~104 bytes.
    """
    base = tempfile.mkdtemp(prefix="phonon-", dir="/tmp")
    return Endpoint(os.path.join(base, "model.sock"), os.urandom(32))


def remove_endpoint(endpoint: Endpoint) -> None:
    shutil.rmtree(os.path.dirname(endpoint.path), ignore_errors=True)


def listen(endpoint: Endpoint) -> Listener:
    """No authkey here on purpose: Listener.accept() would run the handshake on
    the accepting thread, so one peer that stalls mid-handshake would block
    every later front-end. `serve()` authenticates each peer on its own thread."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(endpoint.path)
    return Listener(endpoint.path, family="AF_UNIX", backlog=64)


def serve(
    listener: Listener,
    authkey: bytes,
    on_connection: Callable[[Connection], None],
    handshake_timeout_s: float = 5.0,
) -> None:
    """Accept forever. Each peer is authenticated on its own thread with a
    timeout; only authenticated connections reach `on_connection`."""
    while True:
        try:
            conn = listener.accept()
        except OSError as exc:
            if exc.errno in _TRANSIENT_ACCEPT_ERRORS:
                # e.g. out of file descriptors for a moment: not a reason to take
                # down the model process and every call on the GPU.
                logger.warning(f"accept failed, retrying: {exc!r}")
                time.sleep(0.1)
                continue
            return  # the listener was closed
        except AttributeError:
            return  # Listener.close() already ran (its socket is None)
        threading.Thread(
            target=_handshake,
            args=(conn, authkey, on_connection, handshake_timeout_s),
            name="ipc-handshake",
            daemon=True,
        ).start()


def _handshake(
    conn: Connection,
    authkey: bytes,
    on_connection: Callable[[Connection], None],
    timeout_s: float,
) -> None:
    try:
        _set_io_timeout(conn, timeout_s)
        deliver_challenge(conn, authkey)
        answer_challenge(conn, authkey)
        _set_io_timeout(conn, 0)  # 0 = block forever again
    except Exception as exc:  # bad key, peer gone, or the timeout
        logger.warning(f"rejected an IPC connection: {exc!r}")
        conn.close()  # this thread is its only user
        return
    on_connection(conn)


def _set_io_timeout(conn: Connection, seconds: float) -> None:
    """A kernel-level read/write timeout, so it also applies to Connection's
    raw os.read/os.write calls."""
    tv = struct.pack("ll", int(seconds), int((seconds % 1) * 1_000_000))
    with socket.socket(fileno=os.dup(conn.fileno())) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, tv)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, tv)


def connect(endpoint: Endpoint) -> Connection:
    return Client(endpoint.path, family="AF_UNIX", authkey=endpoint.authkey)


def shutdown(conn: Connection) -> None:
    """Wake every thread using this connection: recv() returns EOF, send() fails
    with OSError. Safe from any thread.

    It never close()s: closing from a thread that doesn't own the connection
    races with a send() in progress (CPython re-reads the handle and raises
    TypeError) and a recv() that could then read a reused file descriptor
    belonging to another connection. Only the owning thread calls close().
    """
    with contextlib.suppress(OSError, ValueError):
        sock = socket.socket(fileno=os.dup(conn.fileno()))
        with sock:
            sock.shutdown(socket.SHUT_RDWR)
