"""Warm pool of persistent Cartesia streaming WebSocket connections.

Opening a fresh WSS connection per cache miss costs a full handshake
(TCP + TLS + upgrade ≈ 2-3 RTT) every sentence. Instead we keep a small set
of sockets open and reuse them: Cartesia multiplexes many utterances over one
connection, tagging each response chunk with the ``context_id`` we send.

Design:

- :class:`_CartesiaConnection` owns one socket and a background ``run()`` task
  that connects, demultiplexes incoming messages by ``context_id`` into
  per-utterance queues, and **reconnects automatically** on drop (Cartesia
  closes idle sockets after ~5 min; the worker self-heals back to ready).
- :class:`CartesiaStreamPool` holds N such connections, picks the least-loaded
  ready one per utterance, and is warmed at startup so misses find a live
  socket immediately.
- The connector is injectable (``connect_fn``) so the routing/reconnect logic
  is unit-tested with a fake socket instead of the network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncGenerator
import time
from typing import Any, Callable

from websockets import connect

from app.core.logging import logger
from app.providers.base import ProviderError

_CARTESIA_WS_URL = "wss://api.cartesia.ai/tts/websocket"

# A connector returns an async context manager whose value is a websocket-like
# object (``await ws.send(str)``, ``async for msg in ws`` -> str, ``await ws.close()``).
ConnectFn = Callable[[str], Any]


class SocketUnavailable(Exception):
    """Raised when no warm Cartesia socket is ready within the acquire window.

    Distinct from a per-utterance error so the caller can fall back to the
    one-shot HTTP synth path instead of failing the request.
    """


class _IPv4Connect:
    """Wraps ``websockets.connect`` to force IPv4, loop-independent.

    On networks where api.cartesia.ai advertises AAAA records but the IPv6 SYN
    is black-holed, the WS library (esp. under uvloop) tries IPv6 first and
    hangs until open_timeout. We resolve an IPv4 address ourselves and hand
    websockets an already-connected IPv4 socket (``sock=``); it still does TLS
    with the correct SNI (``server_hostname`` from the URI) but skips the loop's
    DNS, so this works under both asyncio and uvloop.
    """

    def __init__(self, uri: str):
        self._uri = uri
        self._cm = None

    def _connect_ipv4_socket(self):
        import socket
        from urllib.parse import urlparse

        host = urlparse(self._uri).hostname or "api.cartesia.ai"
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        family, type_, proto, _, sockaddr = infos[0]
        sock = socket.socket(family, type_, proto)
        sock.settimeout(5.0)
        sock.connect(sockaddr)
        sock.settimeout(None)
        return sock

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        # Blocking resolve+connect off the event loop.
        sock = await loop.run_in_executor(None, self._connect_ipv4_socket)
        cm = connect(self._uri, open_timeout=5.0, sock=sock)
        try:
            ws = await cm.__aenter__()
        except BaseException:
            sock.close()  # handshake failed before the transport took ownership
            raise
        self._cm = cm  # success: the transport now owns + closes the socket
        return ws

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


def _default_connect(uri: str):
    """Production connector: short open_timeout so an unreachable WS trips the
    circuit breaker quickly instead of hanging for the default 10s."""
    from app.core.config import settings

    if settings.cartesia_ws_force_ipv4:
        return _IPv4Connect(uri)
    return connect(uri, open_timeout=5.0)


# Per-context queue sentinels (a Queue holds bytes chunks and these markers).
_DONE = object()


class _Err:
    """Carries a ProviderError so a dead connection fails its waiters."""

    __slots__ = ("exc",)

    def __init__(self, exc: Exception):
        self.exc = exc


class _CartesiaConnection:
    """One persistent Cartesia socket + its receive/reconnect loop."""

    def __init__(self, uri: str, connect_fn: ConnectFn):
        self._uri = uri
        self._connect_fn = connect_fn
        self.ws: Any = None
        self.ready = asyncio.Event()
        self.contexts: dict[str, asyncio.Queue] = {}
        self.inflight = 0
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False

    async def run(self) -> None:
        """Connect, receive, and reconnect on drop until ``stop()``.

        Cartesia tears down idle sockets (~5 min); on any close/error we fail
        the in-flight utterances on this socket and reconnect so the pool
        self-heals to a ready state.
        """
        backoff = 0.5
        while not self._closed:
            try:
                async with self._connect_fn(self._uri) as ws:
                    self.ws = ws
                    self.ready.set()
                    backoff = 0.5
                    logger.info("Cartesia stream socket ready")
                    async for message in ws:
                        self._dispatch(message)
                # Socket closed cleanly (e.g. idle timeout) — fail waiters, retry.
                self._mark_all_dead("cartesia socket closed")
            except Exception as e:  # reconnect on any connection failure
                # CancelledError is BaseException -> propagates, stopping the task.
                self._mark_all_dead(f"cartesia socket error: {e}")
            finally:
                self.ready.clear()
                self.ws = None

            if self._closed:
                break
            logger.debug(f"Cartesia socket dropped; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    def _dispatch(self, message: str) -> None:
        """Route one Cartesia message to its context's queue."""
        try:
            m = json.loads(message)
        except (ValueError, TypeError):
            return
        ctx = m.get("context_id")
        q = self.contexts.get(ctx) if ctx else None
        if q is None:
            return  # unknown/already-finished context; ignore
        msg_type = m.get("type")
        if msg_type == "chunk":
            q.put_nowait(base64.b64decode(m["data"]))
        elif msg_type == "done":
            q.put_nowait(_DONE)
        elif msg_type == "error":
            q.put_nowait(_Err(ProviderError(f"cartesia stream error: {m}")))

    def _mark_all_dead(self, reason: str) -> None:
        """Fail every utterance currently bound to this socket."""
        err = _Err(ProviderError(reason))
        for q in list(self.contexts.values()):
            q.put_nowait(err)

    async def send(self, message: str) -> None:
        """Send one message (serialized per socket)."""
        async with self._send_lock:
            if self.ws is None:
                raise ProviderError("cartesia socket not ready")
            await self.ws.send(message)

    async def stop(self) -> None:
        self._closed = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()


class CartesiaStreamPool:
    """A warm pool of persistent Cartesia sockets multiplexed by context_id."""

    def __init__(
        self,
        api_key: str,
        version: str = "2024-06-10",
        min_size: int = 2,
        connect_fn: ConnectFn | None = None,
        acquire_timeout: float = 5.0,
        failure_threshold: int = 2,
        cooldown: float = 60.0,
    ):
        if not api_key:
            raise ValueError("CartesiaStreamPool requires an api_key")
        self._uri = f"{_CARTESIA_WS_URL}?api_key={api_key}&cartesia_version={version}"
        self._min_size = min_size
        self._connect_fn = connect_fn or _default_connect
        self._conns: list[_CartesiaConnection] = []
        self._lock = asyncio.Lock()
        self._started = False
        # Circuit breaker: if WS can't be reached, fail fast so misses fall
        # back to one-shot HTTP synth instead of hanging every request.
        self._acquire_timeout = acquire_timeout
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._acquire_failures = 0
        self._cooldown_until = 0.0

    async def start(self) -> None:
        """Spawn ``min_size`` connection workers (non-blocking; they warm async)."""
        if self._started:
            return
        self._started = True
        for _ in range(self._min_size):
            await self._add_connection()
        logger.info(f"Cartesia stream pool warming {self._min_size} socket(s)")

    async def _add_connection(self) -> None:
        conn = _CartesiaConnection(self._uri, self._connect_fn)
        conn._task = asyncio.create_task(conn.run())
        self._conns.append(conn)

    async def acquire(self) -> _CartesiaConnection:
        """Return the least-loaded ready socket, waiting until one is warm."""
        async with self._lock:
            if not self._conns:
                await self._add_connection()
        # Fast path: a socket is already warm -> WS is up; clear any stale breaker.
        ready = [c for c in self._conns if c.ready.is_set()]
        if ready:
            self._acquire_failures = 0
            return min(ready, key=lambda c: c.inflight)
        # Breaker open (WS confirmed unreachable recently) -> fail fast so the
        # caller falls back to HTTP instead of waiting out acquire_timeout.
        if time.monotonic() < self._cooldown_until:
            raise SocketUnavailable("cartesia WS unavailable (circuit open)")
        # Wait briefly for a worker to finish warming a socket.
        deadline = time.monotonic() + self._acquire_timeout
        while time.monotonic() < deadline:
            ready = [c for c in self._conns if c.ready.is_set()]
            if ready:
                self._acquire_failures = 0
                return min(ready, key=lambda c: c.inflight)
            await asyncio.sleep(0.05)
        # No socket warmed in time -> count a failure, maybe open the breaker.
        self._acquire_failures += 1
        if self._acquire_failures >= self._failure_threshold:
            self._cooldown_until = time.monotonic() + self._cooldown
            logger.warning(
                f"Cartesia WS unreachable after {self._acquire_failures} attempt(s) "
                f"— streaming misses fall back to one-shot HTTP synth for {self._cooldown:.0f}s"
            )
        raise SocketUnavailable("no warm cartesia socket within acquire timeout")

    async def stream(self, msg: dict) -> AsyncGenerator[bytes, None]:
        """Send one utterance (``msg`` minus context_id) over a warm socket.

        Yields audio chunks until Cartesia signals ``done``. On early close
        (consumer cancellation), sends Cartesia a best-effort cancel so it
        stops synthesizing the abandoned utterance.
        """
        conn = await self.acquire()
        ctx_id = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        conn.contexts[ctx_id] = q
        conn.inflight += 1
        completed = False
        try:
            await conn.send(json.dumps({**msg, "context_id": ctx_id}))
            while True:
                item = await q.get()
                if item is _DONE:
                    completed = True
                    break
                if isinstance(item, _Err):
                    raise item.exc
                yield item
        finally:
            conn.contexts.pop(ctx_id, None)
            conn.inflight -= 1
            if not completed:
                # Abandoned mid-synthesis (e.g. caller barge-in): tell Cartesia.
                try:
                    await conn.send(json.dumps({"context_id": ctx_id, "cancel": True}))
                except Exception:
                    pass

    async def aclose(self) -> None:
        for conn in self._conns:
            await conn.stop()
        self._conns.clear()
        self._started = False
