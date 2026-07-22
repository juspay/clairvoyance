"""Warm pool of persistent ElevenLabs streaming WebSocket connections.

ElevenLabs' Multi-Context WebSocket
(``wss://<host>/v1/text-to-speech/{voice_id}/multi-stream-input`` where ``<host>``
is derived from the provider's ``base_url`` — https->wss — so the Indian-residency
instance streams against ``wss://api.in.residency.elevenlabs.io``) is a direct
analog of Cartesia's pool: ONE socket carries up to 5 concurrent
contexts, each tagged with a ``context_id`` we send and echoed back on every
audio frame (base64 ``audio`` + ``isFinal`` end marker). So we keep a small set
of sockets warm and reuse them, mirroring :mod:`app.providers.cartesia_pool`.

Differences from Cartesia (encoded here):
- The voice is in the URL, so a pool is **per voice** (the provider keeps one
  pool per ``voice_id``).
- ``xi-api-key`` header auth (not a query param).
- ``model_id`` and ``output_format`` are **connect-time query params** (they
  cannot vary per utterance on a warm socket) — the pool is therefore keyed by
  ``(voice_id, model_id)``. We set ``output_format=pcm_16000`` (the native cache
  rate) and ``auto_mode=true`` (reduces latency for full phrases).
- Hard **5-context cap per socket** (server-enforced); ``acquire`` skips a socket
  already at the cap and will open another up to ``max_size``.
- ``eleven_v3`` is not supported on the multi-context socket — callers must use a
  v2 model (e.g. ``eleven_flash_v2_5``); this is the caller's responsibility.
- A complete utterance is sent as ``{text, voice_settings, context_id}`` then a
  ``{context_id, flush: true}`` to trigger generation; ``is_final`` ends it and a
  ``{context_id, close_context: true}`` frees the server-side context.

NOTE: this follows ElevenLabs' published multi-context protocol exactly (context_id
routing, base64 ``audio``, ``is_final``). The pool *logic* (warm/reconnect/demux/
breaker/cap) is unit-tested with a fake socket; the live wire schema needs a
smoke-test with a real key on first deploy.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Callable
from urllib.parse import quote

from websockets import connect

from app.core.logging import logger
from app.providers.base import ProviderError

# Path appended to the WS host. The host is derived from the provider's
# base_url (https->wss) so the Indian-residency instance streams against
# wss://api.in.residency.elevenlabs.io rather than the global endpoint.
_ELEVENLABS_WS_PATH = "/v1/text-to-speech/{voice_id}/multi-stream-input"

# A connector returns an async context manager whose value is a websocket-like
# object (``await ws.send(str)``, ``async for msg in ws`` -> str, ``await ws.close()``).
ConnectFn = Callable[[str, dict], Any]

# Per-context queue sentinels.
_DONE = object()


class _Err:
    """Carries a ProviderError so a dead connection fails its waiters."""

    __slots__ = ("exc",)

    def __init__(self, exc: Exception):
        self.exc = exc


class SocketUnavailable(Exception):
    """Raised when no warm ElevenLabs socket is ready within the acquire window.

    Distinct from a per-utterance error so the caller can fall back to the
    one-shot HTTP synth path instead of failing the request.
    """


def _default_connect(uri: str, headers: dict):
    """Production connector: short open_timeout so an unreachable WS trips the
    circuit breaker quickly instead of hanging."""
    return connect(uri, additional_headers=headers, open_timeout=5.0)


# Max concurrent contexts ElevenLabs allows on one multi-context socket.
_MAX_CONTEXTS_PER_SOCKET = 5


class _ElevenLabsConnection:
    """One persistent ElevenLabs socket + its receive/reconnect loop."""

    def __init__(self, uri: str, headers: dict, connect_fn: ConnectFn):
        self._uri = uri
        self._headers = headers
        self._connect_fn = connect_fn
        self.ws: Any = None
        self.ready = asyncio.Event()
        self.contexts: dict[str, asyncio.Queue] = {}
        self.inflight = 0
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False

    async def run(self) -> None:
        """Connect, receive, and reconnect on drop until ``stop()``."""
        backoff = 0.5
        while not self._closed:
            try:
                async with self._connect_fn(self._uri, self._headers) as ws:
                    self.ws = ws
                    self.ready.set()
                    backoff = 0.5
                    logger.info("ElevenLabs stream socket ready")
                    async for message in ws:
                        self._dispatch(message)
                self._mark_all_dead("elevenlabs socket closed")
            except Exception as e:  # reconnect on any connection failure
                # CancelledError is BaseException -> propagates, stopping the task.
                self._mark_all_dead(f"elevenlabs socket error: {e}")
            finally:
                self.ready.clear()
                self.ws = None

            if self._closed:
                break
            logger.debug(f"ElevenLabs socket dropped; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    def _dispatch(self, message: str) -> None:
        """Route one ElevenLabs message to its context's queue."""
        try:
            m = json.loads(message)
        except (ValueError, TypeError):
            return
        ctx = m.get("context_id") or m.get("contextId")
        q = self.contexts.get(ctx) if ctx else None
        if q is None:
            return  # unknown/already-finished context; ignore
        audio_b64 = m.get("audio")
        if audio_b64:
            try:
                q.put_nowait(base64.b64decode(audio_b64))
            except Exception:
                pass
        if m.get("isFinal") or m.get("is_final"):
            q.put_nowait(_DONE)

    def _mark_all_dead(self, reason: str) -> None:
        err = _Err(ProviderError(reason))
        for q in list(self.contexts.values()):
            q.put_nowait(err)

    async def send(self, message: str) -> None:
        """Send one message (serialized per socket)."""
        async with self._send_lock:
            if self.ws is None:
                raise ProviderError("elevenlabs socket not ready")
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


class ElevenLabsStreamPool:
    """A warm pool of persistent ElevenLabs sockets for ONE voice, multiplexed
    by ``context_id``. The provider keeps one pool per ``voice_id``."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model_id: str,
        base_url: str = "https://api.elevenlabs.io",
        *,
        output_format: str = "pcm_16000",
        connect_fn: ConnectFn | None = None,
        min_size: int = 2,
        max_size: int = 8,
        acquire_timeout: float = 5.0,
        failure_threshold: int = 2,
        cooldown: float = 60.0,
        # Seconds of server silence AFTER audio starts that we treat as
        # end-of-utterance. ElevenLabs parks the context ~20s (or
        # inactivity_timeout) past the last chunk waiting for more text and only
        # then emits is_final, so waiting on is_final stalls ~20-120s. A short
        # idle gap is the real end-of-speech signal (mirrors pipecat's
        # stop_frame_timeout).
        idle_timeout: float = 0.8,
    ):
        if not api_key:
            raise ValueError("ElevenLabsStreamPool requires an api_key")
        if not voice_id:
            raise ValueError("ElevenLabsStreamPool requires a voice_id")
        if not model_id:
            raise ValueError("ElevenLabsStreamPool requires a model_id")
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        # output_format + model_id are connect-time query params (the socket's
        # format/model is fixed for its lifetime). auto_mode=true lowers latency
        # for complete phrases; inactivity_timeout bumped so warm sockets survive
        # idle gaps between bursts of misses.
        # WS host mirrors the HTTP base_url host (https->wss), so a residency
        # instance streams against the India endpoint, not the global one.
        ws_host = (
            base_url.replace("https://", "wss://")
            .replace("http://", "ws://")
            .rstrip("/")
        )
        self._uri = (
            f"{ws_host}{_ELEVENLABS_WS_PATH.format(voice_id=voice_id)}"
            f"?model_id={quote(model_id, safe='')}"
            f"&output_format={quote(output_format, safe='')}"
            f"&auto_mode=true&inactivity_timeout=120"
        )
        self._headers = {"xi-api-key": api_key}
        self._connect_fn = connect_fn or _default_connect
        self._min_size = min_size
        self._max_size = max_size
        self._conns: list[_ElevenLabsConnection] = []
        self._lock = asyncio.Lock()
        self._started = False
        self._acquire_timeout = acquire_timeout
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._idle_timeout = idle_timeout
        self._acquire_failures = 0
        self._cooldown_until = 0.0

    @property
    def voice_id(self) -> str:
        return self._voice_id

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for _ in range(self._min_size):
            await self._add_connection()
        logger.info(f"ElevenLabs stream pool warming {self._min_size} socket(s) for voice={self._voice_id}")

    async def _add_connection(self) -> None:
        conn = _ElevenLabsConnection(self._uri, self._headers, self._connect_fn)
        conn._task = asyncio.create_task(conn.run())
        self._conns.append(conn)

    def _available(self) -> list[_ElevenLabsConnection]:
        """Ready sockets with a free context slot (under the 5-context cap)."""
        return [c for c in self._conns if c.ready.is_set() and c.inflight < _MAX_CONTEXTS_PER_SOCKET]

    async def acquire(self) -> _ElevenLabsConnection:
        """Return the least-loaded ready socket with a free context slot,
        opening a new one up to ``max_size`` if all are saturated/cold.

        Reserves the context slot (``inflight++``) under the lock so concurrent
        acquires can't all pick the same socket and push it past the hard
        5-context cap (the server would reject the overflow context).
        """
        async with self._lock:
            avail = self._available()
            if not avail and len(self._conns) < self._max_size:
                await self._add_connection()
                avail = self._available()
            if avail:
                self._acquire_failures = 0
                conn = min(avail, key=lambda c: c.inflight)
                conn.inflight += 1  # reserve the slot NOW (under the lock)
                return conn

        if time.monotonic() < self._cooldown_until:
            raise SocketUnavailable("elevenlabs WS unavailable (circuit open)")

        deadline = time.monotonic() + self._acquire_timeout
        while time.monotonic() < deadline:
            async with self._lock:
                avail = self._available()
                if avail:
                    self._acquire_failures = 0
                    conn = min(avail, key=lambda c: c.inflight)
                    conn.inflight += 1
                    return conn
            await asyncio.sleep(0.05)

        self._acquire_failures += 1
        if self._acquire_failures >= self._failure_threshold:
            self._cooldown_until = time.monotonic() + self._cooldown
            logger.warning(
                f"ElevenLabs WS unreachable after {self._acquire_failures} attempt(s) "
                f"— streaming misses fall back to one-shot HTTP synth for {self._cooldown:.0f}s"
            )
        raise SocketUnavailable("no warm elevenlabs socket within acquire timeout")

    async def stream(self, msg: dict) -> AsyncGenerator[bytes, None]:
        """Send one complete utterance over a warm socket and yield its audio.

        ``msg`` carries ``text`` + ``voice_settings`` (no context_id). The pool
        injects a unique ``context_id`` and runs pipecat's multi-stream-input
        sequence: init (a bare space + voice_settings) -> the real text ->
        ``flush`` (mirrors pipecat's ``ElevenLabsTTSService``). Audio chunks
        (base64 ``audio``) are routed back via a per-context queue. ElevenLabs
        does NOT emit ``is_final`` promptly — it parks the context ~20s (or
        ``inactivity_timeout``) past the last audio chunk waiting for more text
        (confirmed by a raw-frame probe), so we end the stream on a short idle
        gap after audio starts; ``is_final`` still ends us early if it arrives.
        On completion (or early cancellation) we send ``close_context`` so
        ElevenLabs frees the server-side context; the socket stays warm for others.
        """
        conn = await self.acquire()  # reserves a context slot (inflight++) under the lock
        ctx_id = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        conn.contexts[ctx_id] = q
        try:
            # pipecat's multi-stream-input sequence: 1) init the context with a
            # bare space + voice_settings, 2) send the real text, 3) flush.
            init = {"text": " ", "context_id": ctx_id}
            if msg.get("voice_settings"):
                init["voice_settings"] = msg["voice_settings"]
            await conn.send(json.dumps(init))
            await conn.send(json.dumps({"text": msg.get("text", ""), "context_id": ctx_id}))
            await conn.send(json.dumps({"context_id": ctx_id, "flush": True}))
            # ElevenLabs does NOT emit is_final promptly — it parks the context
            # ~20s (or inactivity_timeout) after the last audio chunk waiting for
            # more text (probe + pipecat's own comment confirm this). So we end
            # the stream on a short idle gap AFTER audio starts; is_final still
            # ends us early if it happens to arrive. The first frame gets a
            # longer timeout so a slow cold-start doesn't false-positive.
            got_audio = False
            while True:
                timeout = self._idle_timeout if got_audio else 10.0
                try:
                    item = await asyncio.wait_for(q.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if not got_audio:
                        raise ProviderError(
                            "elevenlabs stream timed out waiting for first audio chunk"
                        )
                    break  # silence after audio => end of utterance
                if item is _DONE:
                    break
                if isinstance(item, _Err):
                    raise item.exc
                got_audio = True
                yield item
        finally:
            conn.contexts.pop(ctx_id, None)
            conn.inflight -= 1
            # Free the server-side context (best effort); the socket stays warm.
            try:
                await conn.send(json.dumps({"context_id": ctx_id, "close_context": True}))
            except Exception:
                pass

    async def aclose(self) -> None:
        for conn in self._conns:
            await conn.stop()
        self._conns.clear()
        self._started = False
