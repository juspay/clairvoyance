"""Fork Daily voice bots from a pre-imported process instead of spawning them.

Spawning re-executes the whole import graph per call (~3.5 CPU-seconds). That
is pure CPU, and the voice pod shares one core with the in-process telephony
pipelines, so it degrades super-linearly: 3.5s at 1.0 core, 19s at 0.25, 48s
at 0.15. A forked child inherits the imports via copy-on-write and runs none
of that work — measured 27.8s -> 0.09s at 0.25 cores.

Gated on BB_DAILY_BOT_ZYGOTE (default True); flip it to False to fall back to
spawning. A failure before the payload reaches the zygote returns None and the
caller falls back on its own; after it, AmbiguousLaunch is raised instead,
because a bot may already be running and a second one would join the same
room.

Incompatible with UVICORN_RELOAD (now off by default): the reloader runs the
server in a subprocess that imports this module fresh and cannot reach the
socket.
"""

import asyncio
import os
import select
import signal
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Optional

from app.core.logger import logger


class AmbiguousLaunch(RuntimeError):
    """The zygote may already have forked a bot; retrying would double-launch."""


# Wire format: 4-byte length + JSON payload; reply is a 4-byte pid.
_LEN = struct.Struct("!I")
_PID = struct.Struct("!I")
_FORK_FAILED = 0xFFFFFFFF

_SPAWN_TIMEOUT_SECS = 10.0

# Set by start_zygote() in the API process; None means every launch falls back.
_sock: Optional[socket.socket] = None
_pid: Optional[int] = None
_lock: Optional[asyncio.Lock] = None


@dataclass
class BotHandle:
    """Reference to one forked bot, reuse-safe where pidfd is available."""

    pid: int
    fd: Optional[int] = None

    def is_alive(self) -> bool:
        if self.fd is not None:
            # A pidfd becomes readable exactly when the process exits, and
            # names this process only — a recycled pid cannot fool it.
            try:
                readable, _, _ = select.select([self.fd], [], [], 0)
                return not readable
            except OSError:
                return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill(self) -> None:
        try:
            sender = getattr(signal, "pidfd_send_signal", None)
            if self.fd is not None and sender is not None:
                sender(self.fd, signal.SIGKILL)
            else:
                os.kill(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def _open_pidfd(pid: int) -> Optional[int]:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return opener(pid)
    except (OSError, ProcessLookupError):
        return None


def _send_all(sock: socket.socket, data: bytes) -> None:
    sock.sendall(data)


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("zygote socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _run_one_bot(raw: bytes) -> None:
    """Grandchild entry: run one call, then exit. Never returns."""
    # Imported here so the API process can import this module without the agent.
    from app.ai.voice.agents.breeze_buddy.services.daily.bot_runner import (
        _amain,
        _parse_payload,
    )

    try:
        runner_args = _parse_payload(raw.decode())
    except ValueError as exc:
        logger.error(f"[zygote] invalid launch payload: {exc}")
        os._exit(2)

    code = 0
    try:
        asyncio.run(_amain(runner_args))
    except BaseException as exc:  # noqa: BLE001 - one bot must not kill the zygote
        logger.opt(exception=True).error(f"[zygote] bot failed: {exc!r}")
        code = 1
    finally:
        # os._exit skips atexit/gc so a wedged native thread cannot keep a
        # finished bot alive.
        os._exit(code)


def _zygote_loop(sock: socket.socket) -> None:
    """Zygote entry: import once, then fork a grandchild per payload."""
    # Kernel-reaped children never become zombies, keeping liveness honest.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        import app.ai.voice.agents.breeze_buddy.agent  # noqa: F401

        logger.info("[zygote] agent imported; ready to fork bots")
    except BaseException as exc:  # noqa: BLE001
        logger.opt(exception=True).error(f"[zygote] import failed, exiting: {exc!r}")
        os._exit(3)

    while True:
        try:
            (length,) = _LEN.unpack(_read_exactly(sock, _LEN.size))
            raw = _read_exactly(sock, length)
        except (ConnectionError, OSError):
            logger.info("[zygote] parent went away; exiting")
            os._exit(0)

        try:
            child = os.fork()
        except OSError as exc:
            logger.error(f"[zygote] fork failed: {exc}")
            child = _FORK_FAILED
        if child == 0:
            sock.close()
            # Own session: a signal to the API's group can't kill a live call.
            os.setsid()
            _run_one_bot(raw)
            return

        try:
            sock.sendall(_PID.pack(child))
        except OSError:
            logger.info("[zygote] parent went away while replying; exiting")
            os._exit(0)


def start_zygote() -> bool:
    """Fork the zygote. Call from run.py before uvicorn starts."""
    global _sock, _pid

    if _sock is not None:
        logger.warning("[zygote] already started; ignoring")
        return True
    if not hasattr(os, "fork"):
        logger.warning("[zygote] os.fork unavailable on this platform")
        return False

    # Only the forking thread survives, so a live loop or open pool would be
    # inherited half-broken. loguru's writer threads are fine — it reinstalls
    # them via os.register_at_fork.
    try:
        asyncio.get_running_loop()
        logger.error("[zygote] refusing to fork: an event loop is already running")
        return False
    except RuntimeError:
        pass
    try:
        from app.database import pool as _db_pool

        if _db_pool is not None:
            logger.error("[zygote] refusing to fork: the database pool is open")
            return False
    except Exception:  # noqa: BLE001 - no pool is the safe case
        pass
    logger.debug(f"[zygote] pre-fork threads: {threading.active_count()}")

    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid = os.fork()
    except OSError as exc:
        logger.error(f"[zygote] fork failed, falling back to spawning: {exc}")
        parent_sock.close()
        child_sock.close()
        return False

    if pid == 0:
        parent_sock.close()
        _zygote_loop(child_sock)
        os._exit(0)

    child_sock.close()
    _sock = parent_sock
    _pid = pid
    logger.info(f"[zygote] started (pid={pid})")
    return True


def is_available() -> bool:
    return _sock is not None


async def spawn_via_zygote(payload_json: str) -> Optional[BotHandle]:
    """Fork a bot for ``payload_json``.

    Returns None when nothing was started and the caller may safely fall back.
    Raises AmbiguousLaunch once the payload is on the wire, because the zygote
    forks before it replies: a lost reply means a bot may be running, and
    spawning another would put two bots in one room.
    """
    global _sock, _lock

    sock = _sock
    if sock is None:
        return None
    if _lock is None:
        # The socket carries a request/response pair; interleaving would
        # mismatch pids.
        _lock = asyncio.Lock()

    body = payload_json.encode()
    loop = asyncio.get_running_loop()

    def _drop(dead: socket.socket, exc: BaseException) -> None:
        global _sock
        logger.error(f"[zygote] handoff failed ({exc!r}); disabling zygote")
        _sock = None
        try:
            dead.close()
        except OSError:
            pass

    async with _lock:
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None, _send_all, sock, _LEN.pack(len(body)) + body
                ),
                timeout=_SPAWN_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            # Nothing (or a partial frame) reached the zygote, so it cannot
            # have forked: falling back is safe.
            _drop(sock, exc)
            return None
        try:
            raw = await asyncio.wait_for(
                loop.run_in_executor(None, _read_exactly, sock, _PID.size),
                timeout=_SPAWN_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            _drop(sock, exc)
            raise AmbiguousLaunch(
                "zygote did not return a pid; a bot may already be running"
            ) from exc

    (pid,) = _PID.unpack(raw)
    if pid == _FORK_FAILED:
        logger.error("[zygote] reported a fork failure")
        return None
    return BotHandle(pid=pid, fd=_open_pidfd(pid))


def stop_zygote() -> None:
    """Close the socket so the zygote exits. Safe to call more than once."""
    global _sock, _pid
    sock, _sock = _sock, None
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    if _pid is not None:
        logger.info(f"[zygote] stopped (pid={_pid})")
        _pid = None


__all__ = [
    "AmbiguousLaunch",
    "BotHandle",
    "is_available",
    "spawn_via_zygote",
    "start_zygote",
    "stop_zygote",
]
