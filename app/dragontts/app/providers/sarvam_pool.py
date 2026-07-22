"""Warm pool of persistent Sarvam streaming WebSocket connections.

Sarvam's streaming TTS WebSocket (``wss://api.sarvam.ai/text-to-speech/ws``) is
a **single-stream** protocol: one socket serves one utterance at a time (config
-> text -> flush -> audio chunks -> ``final`` event). Unlike Cartesia /
ElevenLabs there is no per-utterance routing/multiplexing, so we keep a small
LIFO stack of warm, pre-connected sockets and hand one out exclusively per miss,
returning it to the pool when the utterance finishes.

Why: a fresh WSS handshake (TCP + TLS + upgrade) per miss is the dominant miss
latency for a streaming provider. Reusing a warm socket removes it. Sockets are
kept alive with periodic application-level ``ping`` messages (Sarvam closes idle
connections after ~1 minute) and reconnect on drop.

Input frames (client): ``{"type":"config","data":{...}}``,
``{"type":"text","data":{"text":...}}``, ``{"type": "flush"}``,
``{"type": "ping"}``. Output frames (server):
``{"type":"audio","data":{"audio":"<b64>"}}``,
``{"type":"event","data":{"event_type":"final"}}``,
``{"type":"error",...}``.

NOTE: this follows Sarvam's published streaming protocol. The pool *logic*
(exclusive acquire/release, warm/reconnect/keepalive/breaker) is unit-tested
with a fake socket; the live wire schema + ``api-subscription-key`` header auth
need a smoke-test with a real key on first deploy.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator
from typing import Any, Callable

from websockets import connect

from app.core.logging import logger
from app.providers.base import ProviderError

_SARVAM_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"

ConnectFn = Callable[[str, dict], Any]


class SocketUnavailable(Exception):
    """Raised when no warm Sarvam socket is ready within the acquire window.

    Distinct from a per-utterance error so the caller falls back to one-shot
    HTTP synth instead of failing the request.
    """


def _default_connect(uri: str, headers: dict):
    """Production connector: short open_timeout so an unreachable WS trips the
    circuit breaker quickly instead of hanging."""
    return connect(uri, additional_headers=headers, open_timeout=5.0)


class _SarvamConnection:
    """One persistent Sarvam socket + its connect/reconnect/keepalive loop.

    A single socket serves one utterance at a time (the pool guarantees
    exclusive access via :meth:`acquire`/:meth:`release`). Audio for an
    utterance is read directly off the socket in :meth:`stream`; the background
    ``run`` task never reads, so there is exactly one consumer.
    """

    def __init__(
        self,
        uri: str,
        headers: dict,
        connect_fn: ConnectFn,
        ping_interval: float = 25.0,
        recv_timeout: float = 30.0,
    ):
        self._uri = uri
        self._headers = headers
        self._connect_fn = connect_fn
        self._ping_interval = ping_interval
        self._recv_timeout = recv_timeout
        self.ws: Any = None
        self.ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._busy = False  # in an utterance (pool-owned); gates idle pings
        self._dirty = False  # prior utterance aborted non-cleanly -> reconnect

    async def run(self) -> None:
        """Connect, stay alive (idle pings), and reconnect on drop until stop()."""
        backoff = 0.5
        while not self._closed:
            try:
                async with self._connect_fn(self._uri, self._headers) as ws:
                    self.ws = ws
                    self.ready.set()
                    self._dirty = False  # fresh socket carries no stale frames
                    backoff = 0.5
                    logger.info("Sarvam stream socket ready")
                    last_ping = time.monotonic()
                    # Park until the socket closes. While idle (not in an
                    # utterance), send keepalive pings so Sarvam doesn't drop us.
                    while not self._closed:
                        if self._busy:
                            await asyncio.sleep(0.1)
                            continue
                        if self._dirty:
                            # The last utterance on this socket ended without a
                            # clean ``final`` (client cancel / timeout / error
                            # frame). Sarvam is single-stream with no per-
                            # utterance routing, so its leftover frames would be
                            # read as the NEXT caller's audio. Drop this socket
                            # and reconnect to a fresh one (ready was already
                            # cleared by stream()'s finally, so the pool can't
                            # hand it out in the meantime).
                            self._dirty = False
                            break
                        now = time.monotonic()
                        if now - last_ping >= self._ping_interval:
                            last_ping = now
                            try:
                                await self._send({"type": "ping"})
                            except Exception:
                                break  # socket is dead -> reconnect below
                        await asyncio.sleep(1.0)
            except Exception as e:  # reconnect on any connection failure
                # CancelledError is BaseException -> propagates, stopping the task.
                logger.debug(f"sarvam socket error: {e}")
            finally:
                self.ready.clear()
                self.ws = None

            if self._closed:
                break
            logger.debug(f"Sarvam socket dropped; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def _send(self, msg: dict) -> None:
        async with self._send_lock:
            if self.ws is None:
                raise ProviderError("sarvam socket not ready")
            await self.ws.send(json.dumps(msg))

    async def stream(self, config: dict, text: str) -> AsyncGenerator[bytes, None]:
        """Run ONE utterance on this socket: config -> text -> flush -> audio.

        Yields raw PCM bytes (at the model's native rate; the provider resamples
        to 16 kHz) until the ``final`` event. Raises ``ProviderError`` on an
        error frame or a read timeout (a hung server).

        On any exit other than a clean ``final`` (client cancel, timeout, error
        frame) the socket is marked dirty and ``ready`` cleared: ``run`` then
        drops + reconnects it, so Sarvam's leftover frames for THIS utterance
        can never be read as a later caller's audio (single-stream, no per-
        utterance routing — that would silently cross-contaminate cached clips).
        """
        completed = False
        await self._send({"type": "config", "data": config})
        await self._send({"type": "text", "data": {"text": text}})
        await self._send({"type": "flush"})
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=self._recv_timeout)
                except asyncio.TimeoutError as e:
                    raise ProviderError("sarvam stream timed out waiting for audio") from e
                try:
                    m = json.loads(raw)
                except (ValueError, TypeError):
                    continue  # ignore non-JSON / control frames
                mtype = m.get("type")
                if mtype == "audio":
                    audio_b64 = (m.get("data") or {}).get("audio")
                    if audio_b64:
                        try:
                            yield base64.b64decode(audio_b64)
                        except Exception:
                            pass
                elif mtype == "event":
                    if (m.get("data") or {}).get("event_type") == "final":
                        completed = True
                        return
                elif mtype == "error":
                    raise ProviderError(f"sarvam stream error: {m}")
        finally:
            if not completed:
                # Aborted mid-utterance: Sarvam keeps streaming this transcript's
                # audio into the socket buffer. Clearing ``ready`` keeps the pool
                # from handing the socket out, and ``_dirty`` makes ``run`` drop
                # + reconnect it so the next caller gets a clean socket.
                self._dirty = True
                self.ready.clear()

    async def stop(self) -> None:
        self._closed = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()


# bulbul model -> native PCM sample rate it streams at (provider uses this to
# size the resampler; not 16 kHz, so every stream chunk is resampled on the way).
_MODEL_SAMPLE_RATE = {"bulbul:v2": 22050, "bulbul:v3": 24000}


def model_sample_rate(model: str) -> int:
    """Native PCM rate a bulbul model streams at (v2=22050, v3=24000)."""
    return _MODEL_SAMPLE_RATE.get(model, 24000)


class SarvamStreamPool:
    """A warm LIFO pool of persistent Sarvam sockets (one utterance each)."""

    def __init__(
        self,
        api_key: str,
        model: str = "bulbul:v2",
        connect_fn: ConnectFn | None = None,
        min_size: int = 2,
        max_size: int = 8,
        acquire_timeout: float = 5.0,
        failure_threshold: int = 2,
        cooldown: float = 60.0,
    ):
        if not api_key:
            raise ValueError("SarvamStreamPool requires an api_key")
        self._model = model
        self._uri = f"{_SARVAM_WS_URL}?model={model}&send_completion_event=true"
        self._headers = {"api-subscription-key": api_key}
        self._connect_fn = connect_fn or _default_connect
        self._min_size = min_size
        self._max_size = max_size
        self._conns: list[_SarvamConnection] = []
        self._lock = asyncio.Lock()
        self._started = False
        self._acquire_timeout = acquire_timeout
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._acquire_failures = 0
        self._cooldown_until = 0.0

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for _ in range(self._min_size):
            await self._add_connection()
        logger.info(f"Sarvam stream pool warming {self._min_size} socket(s) for model={self._model}")

    async def _add_connection(self) -> _SarvamConnection:
        conn = _SarvamConnection(self._uri, self._headers, self._connect_fn)
        conn._task = asyncio.create_task(conn.run())
        self._conns.append(conn)
        return conn

    def _free_ready(self) -> list[_SarvamConnection]:
        """Ready sockets not currently serving an utterance (LIFO: newest first)."""
        return [c for c in reversed(self._conns) if c.ready.is_set() and not c._busy]

    async def acquire(self) -> _SarvamConnection:
        """Return an exclusive ready socket, growing the pool up to ``max_size``.

        Raises :class:`SocketUnavailable` (after the acquire window / breaker)
        so the caller falls back to one-shot HTTP synth.

        The scan + ``_busy`` reservation is under ``self._lock`` so two callers
        can never both take the same single-stream Sarvam socket — that would
        interleave their config/text/flush frames on one ``ws.recv()`` and
        corrupt the cached audio.
        """
        # Fast path: reserve a ready socket under the lock (scan + mark atomic).
        async with self._lock:
            free = self._free_ready()
            if free:
                self._acquire_failures = 0
                free[0]._busy = True
                return free[0]
            if len(self._conns) < self._max_size:
                await self._add_connection()

        if time.monotonic() < self._cooldown_until:
            raise SocketUnavailable("sarvam WS unavailable (circuit open)")

        deadline = time.monotonic() + self._acquire_timeout
        while time.monotonic() < deadline:
            async with self._lock:
                free = self._free_ready()
                if free:
                    self._acquire_failures = 0
                    free[0]._busy = True
                    return free[0]
            await asyncio.sleep(0.05)

        self._acquire_failures += 1
        if self._acquire_failures >= self._failure_threshold:
            self._cooldown_until = time.monotonic() + self._cooldown
            logger.warning(
                f"Sarvam WS unreachable after {self._acquire_failures} attempt(s) "
                f"— streaming misses fall back to one-shot HTTP synth for {self._cooldown:.0f}s"
            )
        raise SocketUnavailable("no warm sarvam socket within acquire timeout")

    async def stream(self, config: dict, text: str) -> AsyncGenerator[bytes, None]:
        """Acquire an exclusive warm socket, run one utterance, release it.

        On any error the socket is released (not returned broken — the run loop
        will reconnect it) and the exception propagates so the provider can fall
        back to one-shot synth if nothing was streamed yet.
        """
        conn = await self.acquire()
        try:
            async for chunk in conn.stream(config, text):
                yield chunk
        finally:
            conn._busy = False

    async def aclose(self) -> None:
        for conn in self._conns:
            await conn.stop()
        self._conns.clear()
        self._started = False
