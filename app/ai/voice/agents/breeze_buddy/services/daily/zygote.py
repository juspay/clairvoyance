"""Fork Daily voice bots from a pre-imported process instead of spawning them.

Spawning re-executes the whole import graph per call (~3.5 CPU-seconds). That
is pure CPU, and the voice pod shares one core with the in-process telephony
pipelines, so it degrades super-linearly: 3.5s at 1.0 core, 19s at 0.25, 48s
at 0.15. A forked child inherits the imports via copy-on-write and runs none
of that work — measured 27.8s -> 0.09s at 0.25 cores.

Three processes are involved:

    API process   start_zygote() at boot, then spawn_bot() per call
      └─ zygote   imports the agent once, then forks on demand
           └─ bot one call, then exits

The zygote sends a ready byte once the agent is resident. Until the API reads
it, is_available() is False and every launch takes the spawn path: a zygote
still executing its imports cannot answer, and a timed-out handoff fails the
call rather than falling back.

Gated on BB_DAILY_BOT_ZYGOTE, which ships off; setting it back to False is
the escape hatch to spawning. A failure before the payload reaches the zygote
returns None and the caller falls back on its own; after it, AmbiguousLaunch
is raised instead, because a bot may already be running and a second one would
join the same room. Either way the zygote is killed before its socket closes —
queued bytes stay readable after our end is closed, so a live zygote could
otherwise still fork a bot nobody supervises.

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

from app.core.config.static import BB_DAILY_BOT_HANDOFF_TIMEOUT_SECS
from app.core.logger import logger


class AmbiguousLaunch(RuntimeError):
    """The zygote may already have forked a bot; retrying would double-launch."""


# Request framing: a 4-byte big-endian length, then that many bytes of JSON.
# The reply is one 4-byte big-endian pid.
_REQUEST_LENGTH_HEADER = struct.Struct("!I")
_REPLY_PID = struct.Struct("!I")

# Sentinel pid the zygote returns when its own fork() failed.
_FORK_FAILED_SENTINEL = 0xFFFFFFFF

# Sent by the zygote once its agent import has succeeded.
_ZYGOTE_READY_BYTE = b"\x01"

# Set by start_zygote() in the API process. None means "no zygote reachable",
# which every caller treats as "fall back to spawning".
_zygote_socket: Optional[socket.socket] = None
_zygote: Optional["BotHandle"] = None
_zygote_ready = False
_handoff_lock: Optional[asyncio.Lock] = None


def _is_readable(fd: int) -> bool:
    """Non-blocking readability check.

    poll() rather than select(), which raises ValueError — not OSError — for
    descriptors at or above FD_SETSIZE (1024).
    """
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


@dataclass
class BotHandle:
    """A reference to one forked process, reuse-safe where pidfd is available.

    A bare pid cannot distinguish a finished bot from a recycled pid. Acting on
    a stale pid would both leak a capacity slot and eventually SIGKILL an
    unrelated process, so we prefer a pidfd, which names *this* process for as
    long as the fd stays open.
    """

    pid: int
    pidfd: Optional[int] = None

    def is_running(self) -> bool:
        if self.pidfd is not None:
            # A pidfd becomes readable exactly when the process exits.
            try:
                return not _is_readable(self.pidfd)
            except (OSError, ValueError):
                return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate(self) -> None:
        """Best-effort SIGKILL, targeted at this exact process."""
        try:
            send_to_pidfd = getattr(signal, "pidfd_send_signal", None)
            if self.pidfd is not None and send_to_pidfd is not None:
                send_to_pidfd(self.pidfd, signal.SIGKILL)
            else:
                os.kill(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def release(self) -> None:
        """Close the pidfd. Safe to call more than once."""
        if self.pidfd is not None:
            try:
                os.close(self.pidfd)
            except OSError:
                pass
            self.pidfd = None


def _open_pidfd(pid: int) -> Optional[int]:
    """Return a pidfd for ``pid``, or None when unsupported or already gone."""
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None:
        return None
    try:
        return pidfd_open(pid)
    except (OSError, ProcessLookupError):
        return None


def _receive_exactly(sock: socket.socket, byte_count: int) -> bytes:
    """Blocking read of exactly ``byte_count`` bytes. Zygote side only."""
    chunks = []
    remaining = byte_count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("zygote socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


async def _receive_exactly_async(
    loop: asyncio.AbstractEventLoop, sock: socket.socket, byte_count: int
) -> bytes:
    """Read exactly ``byte_count`` bytes without blocking the event loop."""
    chunks = []
    remaining = byte_count
    while remaining:
        chunk = await loop.sock_recv(sock, remaining)
        if not chunk:
            raise ConnectionError("zygote socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _run_bot_until_exit(request: bytes) -> None:
    """Bot process entry: run one call, then exit. Never returns."""
    # Must come first. loguru's os.register_at_fork hook only re-arms handler
    # locks — the writer thread is not recreated in the child, so without this
    # the bot would enqueue into the API process's queue, and a SIGKILL during
    # a put would leave that queue's semaphore held and wedge every process.
    from app.core.logger import configure_session_logger

    configure_session_logger(session_id=f"bot-{os.getpid()}")

    # Imported here so the API process can import this module without pulling
    # in the agent; only the bot process needs it, and by then it is already
    # resident from the zygote's own import.
    from app.ai.voice.agents.breeze_buddy.services.daily.bot_runner import (
        _amain,
        _parse_payload,
    )

    try:
        runner_args = _parse_payload(request.decode())
    except ValueError as exc:
        logger.error(f"[zygote] invalid launch payload: {exc}")
        os._exit(2)

    exit_code = 0
    try:
        asyncio.run(_amain(runner_args))
    except BaseException as exc:  # noqa: BLE001 - one bot must not kill the zygote
        logger.opt(exception=True).error(f"[zygote] bot failed: {exc!r}")
        exit_code = 1
    finally:
        # os._exit skips atexit/gc, so a wedged native thread cannot keep a
        # finished bot alive.
        os._exit(exit_code)


def _serve_launch_requests(sock: socket.socket) -> None:
    """Zygote process entry: import once, then fork a bot per request."""
    # Kernel-reaped children never linger as zombies, which is what keeps
    # BotHandle.is_running() honest.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    # The API process kills us explicitly; a Ctrl-C in its terminal must not
    # interrupt a live call.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        # Only the zygote may import the agent. run.py imports this module into
        # the API process, so hoisting this to the top of the file would make
        # every process pay the cost this file exists to pay exactly once.
        import app.ai.voice.agents.breeze_buddy.agent  # noqa: F401
    except BaseException as exc:  # noqa: BLE001
        logger.opt(exception=True).error(f"[zygote] import failed, exiting: {exc!r}")
        os._exit(3)

    try:
        sock.sendall(_ZYGOTE_READY_BYTE)
    except OSError:
        os._exit(0)
    logger.info("[zygote] agent imported; ready to fork bots")

    while True:
        try:
            (length,) = _REQUEST_LENGTH_HEADER.unpack(
                _receive_exactly(sock, _REQUEST_LENGTH_HEADER.size)
            )
            request = _receive_exactly(sock, length)
        except (ConnectionError, OSError):
            logger.info("[zygote] parent went away; exiting")
            os._exit(0)

        try:
            bot_pid = os.fork()
        except OSError as exc:
            logger.error(f"[zygote] fork failed: {exc}")
            bot_pid = _FORK_FAILED_SENTINEL

        if bot_pid == 0:
            try:
                sock.close()
                # Bots fork nothing; inheriting SIG_IGN would silently break
                # any waitpid the call path grows later.
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                # Its own session, so a signal sent to the API's process group
                # cannot interrupt a live call.
                os.setsid()
                _run_bot_until_exit(request)
            except BaseException:  # noqa: BLE001
                logger.opt(exception=True).error("[zygote] bot bootstrap failed")
            finally:
                # A bot must never unwind back into the zygote's loop, nor out
                # of start_zygote() into run.py, where it would fall through to
                # a second uvicorn.run().
                os._exit(1)

        try:
            sock.sendall(_REPLY_PID.pack(bot_pid))
        except OSError:
            logger.info("[zygote] parent went away while replying; exiting")
            os._exit(0)


def _is_safe_to_fork() -> bool:
    """True when forking now would not inherit half-broken state.

    Only the calling thread survives a fork, so a running event loop or an open
    connection pool would come across unusable. loguru's writer threads are
    handled rather than checked: they are not recreated in the child, so each
    bot rebuilds its own sinks via configure_session_logger.
    """
    try:
        asyncio.get_running_loop()
        logger.error("[zygote] refusing to fork: an event loop is already running")
        return False
    except RuntimeError:
        pass

    try:
        # Read inside the function: a module-level `from ... import pool` would
        # snapshot None at import time and never see the pool open.
        from app.database import pool as database_pool

        if database_pool is not None:
            logger.error("[zygote] refusing to fork: the database pool is open")
            return False
    except Exception:  # noqa: BLE001 - no pool yet is the safe case
        pass

    logger.debug(f"[zygote] pre-fork threads: {threading.active_count()}")
    return True


def start_zygote() -> bool:
    """Fork the zygote. Call from run.py before uvicorn starts.

    Returns True when the zygote process exists. It is not usable until it
    reports ready — see is_available(). False means every launch falls back to
    spawning, which is the behaviour this change replaces, so a failure here
    costs performance, never correctness.
    """
    global _zygote_socket, _zygote, _zygote_ready

    if _zygote_socket is not None:
        logger.warning("[zygote] already started; ignoring")
        return True
    if not hasattr(os, "fork"):
        logger.warning("[zygote] os.fork unavailable on this platform")
        return False
    if not _is_safe_to_fork():
        return False

    api_side, zygote_side = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid = os.fork()
    except OSError as exc:
        logger.error(f"[zygote] fork failed, falling back to spawning: {exc}")
        api_side.close()
        zygote_side.close()
        return False

    if pid == 0:
        api_side.close()
        _serve_launch_requests(zygote_side)
        os._exit(0)

    zygote_side.close()
    # Non-blocking so loop.sock_* genuinely cancels on timeout; an executor
    # thread blocked in sendall/recv cannot be cancelled at all.
    api_side.setblocking(False)
    _zygote_socket = api_side
    _zygote_ready = False
    _zygote = BotHandle(pid=pid, pidfd=_open_pidfd(pid))
    logger.info(f"[zygote] started (pid={pid}); waiting for the agent import")
    return True


def is_available() -> bool:
    """True once the zygote has signalled that the agent is resident.

    Reporting availability earlier would send launches to a zygote still
    executing its imports: the handoff would time out and fail the call
    instead of quietly falling back to spawning.
    """
    global _zygote_ready

    sock = _zygote_socket
    if sock is None:
        return False
    if _zygote_ready:
        return True

    try:
        if not _is_readable(sock.fileno()):
            return False
        if sock.recv(1) != _ZYGOTE_READY_BYTE:
            raise ConnectionError("zygote closed before reporting ready")
    except BlockingIOError:
        # Spurious readability on a non-blocking socket; check again next call.
        return False
    except (ValueError, OSError) as exc:
        _disable_zygote(sock, exc)
        return False

    _zygote_ready = True
    logger.info("[zygote] ready; Daily bots will be forked")
    return True


def _disable_zygote(dead_socket: socket.socket, exc: BaseException) -> None:
    """Kill the zygote and drop it so every later launch falls back to spawning.

    Killing precedes closing: queued bytes stay readable after our end is
    closed, so a live zygote could still fork a bot with no supervisor, no
    capacity slot and no watchdog. Bots it already forked have their own
    sessions and are unaffected.
    """
    global _zygote_socket, _zygote, _zygote_ready

    logger.error(f"[zygote] unusable ({exc!r}); disabling zygote")
    _zygote_socket = None
    _zygote_ready = False

    if _zygote is not None:
        _zygote.terminate()
        _zygote.release()
        _zygote = None
    try:
        dead_socket.close()
    except OSError:
        pass


async def spawn_bot(payload_json: str) -> Optional[BotHandle]:
    """Ask the zygote to fork a bot for ``payload_json``.

    Returns None when nothing was started and the caller may safely fall back
    to spawning. Raises AmbiguousLaunch once the payload is on the wire: the
    zygote forks *before* it replies, so a lost reply means a bot may already
    be running, and starting a second one would put two bots in one room.
    """
    global _handoff_lock

    sock = _zygote_socket
    if sock is None:
        return None
    if _handoff_lock is None:
        # One handoff at a time: the socket carries a request/reply pair, and
        # interleaving them would pair a caller with another caller's pid.
        _handoff_lock = asyncio.Lock()

    request = payload_json.encode()
    frame = _REQUEST_LENGTH_HEADER.pack(len(request)) + request
    loop = asyncio.get_running_loop()

    async with _handoff_lock:
        try:
            await asyncio.wait_for(
                loop.sock_sendall(sock, frame),
                timeout=BB_DAILY_BOT_HANDOFF_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            # A cancelled send leaves at most a partial frame, which blocks the
            # zygote mid-read: it cannot have forked. Falling back is safe, and
            # _disable_zygote kills it before a later byte could change that.
            _disable_zygote(sock, exc)
            return None

        try:
            reply = await asyncio.wait_for(
                _receive_exactly_async(loop, sock, _REPLY_PID.size),
                timeout=BB_DAILY_BOT_HANDOFF_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            _disable_zygote(sock, exc)
            raise AmbiguousLaunch(
                "zygote did not return a pid; a bot may already be running"
            ) from exc

    (bot_pid,) = _REPLY_PID.unpack(reply)
    if bot_pid == _FORK_FAILED_SENTINEL:
        logger.error("[zygote] reported a fork failure")
        return None
    return BotHandle(pid=bot_pid, pidfd=_open_pidfd(bot_pid))


__all__ = [
    "AmbiguousLaunch",
    "BotHandle",
    "is_available",
    "spawn_bot",
    "start_zygote",
]
