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
- ``eleven_v3*`` / ``eleven_v4*`` models use a DIFFERENT endpoint: the
  Text-to-Dialogue multi-context socket
  (``/v1/text-to-dialogue/multi-stream-input``), the only way to reach them
  (the text-to-speech socket rejects those model ids). The wire
  protocol differs accordingly: contexts open with a ``voices`` registration
  (voice is NOT in the URL), text travels as ``inputs`` entries, only
  ``stability`` is honored in voice_settings, and the socket auto-closes after
  20s of client silence — so v3 connections register a dedicated keepalive
  context at connect and ping it every ``keepalive_interval`` seconds (mirrors
  pipecat's ``ElevenLabsDialogueTTSService``).
- A complete utterance is sent as ``{text, voice_settings, context_id}`` then a
  ``{context_id, flush: true}`` to trigger generation; ``is_final`` ends it and a
  ``{context_id, close_context: true}`` frees the server-side context. On the
  v3 socket ``is_final_audio_for_turn`` additionally marks a turn's last chunk.

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
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

from websockets import connect
from websockets.exceptions import ConnectionClosed

from app.core.logging import logger
from app.providers import elevenlabs_live as live
from app.providers.base import ProviderError
from app.providers.elevenlabs_live import LIVE

if TYPE_CHECKING:
    from app.providers.elevenlabs_accounts import AccountBudget, ElevenLabsAccount

# Models that accept a language_code query param on the multi-context socket.
# Mirrors pipecat's ELEVENLABS_MULTILINGUAL_MODELS.
_ELEVENLABS_MULTILINGUAL_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5"}


_TTD_MODEL_PREFIXES = ("eleven_v3", "eleven_v4")


def is_elevenlabs_ttd_model(model_id: str | None) -> bool:
    """True for Eleven v3 and v4 models — only reachable via the
    Text-to-Dialogue socket (the text-to-speech multi-context socket rejects
    them: 404 for v3, 400 for v4)."""
    return bool(model_id) and model_id.strip().startswith(_TTD_MODEL_PREFIXES)


def is_elevenlabs_v4_model(model_id: str | None) -> bool:
    """True for Eleven v4 models (``eleven_v4``, ``eleven_v4_turbo``). They
    share the v3 Text-to-Dialogue path. The v4 BASE is served as generated;
    the v4 ``_tempo`` / ``_clean_tempo`` / ``_clean_tempo_v2`` variants run
    the same local chain as v3 conversational's (its thresholds were tuned on
    v3 output)."""
    return bool(model_id) and model_id.strip().startswith("eleven_v4")


# DragonTTS-local pipeline selectors layered ON TOP of a pipeline family's base
# model. ElevenLabs has no such model ids — normalize_pipeline_model strips the
# suffix before any upstream call — they choose the LOCAL processing chain (and
# the generation rate) so a template can A/B chains by model name with zero
# client changes:
#   base          : the family's direct rate (v3 conversational: ElevenLabs'
#                   own pcm_8000; v4: ELEVENLABS_V4_NATIVE_SAMPLE_RATE).
#                   tempo 1 = unaltered; tempo != 1 = atempo only (no hygiene).
#   _tempo        : full-band native rate + atempo, no hygiene; the custom
#                   anti-aliased downsample to the caller's rate happens once
#                   at synth time and the cache stores that end result.
#   _clean_tempo  : full-band native rate + hygiene + end release + atempo.
_V3CONV = "eleven_v3_conversational"
# v4 bases (longest first). Matched EXACTLY — base or base + a known suffix —
# so a future eleven_v4_* model is never mistaken for a variant of eleven_v4.
# v3 conversational keeps its prefix match (any unknown suffix = its base).
_V4_PIPELINE_BASES = ("eleven_v4_turbo", "eleven_v4")
# Classic-socket models with local pipeline variants. ONLY the suffixed ids
# (base + _tempo / _clean_tempo / _clean_tempo_v2) are pipeline models: the
# bare id is NOT — plain eleven_flash_v2_5 keeps today's path exactly
# (format-agnostic cache, live streaming, native speed). Unlike v3/v4, flash
# honors ElevenLabs' own `speed`, so its variants keep speed native and only
# an explicit `tempo` param adds the atempo stretch.
_FLASH_PIPELINE_BASES = ("eleven_flash_v2_5",)
_PIPELINE_SUFFIXES = (
    # _clean_tempo_v2: _clean_tempo + end release, longer sentence tail and one
    # loudness per sentence, so cached sentences join like one speaker
    # (app/audio/join.py, app/audio/level.py).
    "_clean_tempo_v2",
    "_clean_tempo",
    "_tempo",
)  # longest first for stripping


def _split_pipeline_model(model_id: str | None) -> tuple[str, str] | None:
    """(base model, variant) for a pipeline-family id, else None."""
    m = (model_id or "").strip().lower()
    if m.startswith(_V3CONV):
        for suffix in _PIPELINE_SUFFIXES:
            if m == _V3CONV + suffix:
                return _V3CONV, suffix.lstrip("_")
        return _V3CONV, "base"
    for base in _V4_PIPELINE_BASES:
        if m == base:
            return base, "base"
        for suffix in _PIPELINE_SUFFIXES:
            if m == base + suffix:
                return base, suffix.lstrip("_")
    for base in _FLASH_PIPELINE_BASES:
        for suffix in _PIPELINE_SUFFIXES:  # suffixed ids only, never the bare base
            if m == base + suffix:
                return base, suffix.lstrip("_")
    return None


def has_local_pipeline(model_id: str | None) -> bool:
    """True for the models that carry DragonTTS's local pipeline — the v3
    *Conversational* family and the v4 family (``eleven_v4_turbo``,
    ``eleven_v4``), with or without a variant suffix, and the SUFFIXED flash
    ids (``eleven_flash_v2_5_tempo`` / ``_clean_tempo`` / ``_clean_tempo_v2``).
    Only these ever take the ffmpeg atempo stage; plain ``eleven_v3``, bare
    flash and v2 models are unaffected even when a tempo param is sent (the
    cache layer strips it before keying for them)."""
    return _split_pipeline_model(model_id) is not None


def is_flash_pipeline_model(model_id: str | None) -> bool:
    """True for the suffixed flash variants (classic socket + local chain)."""
    return pipeline_family(model_id) in _FLASH_PIPELINE_BASES


def needs_whole_clip(model_id: str | None) -> bool:
    """True for the variants whose chain needs the WHOLE clip (hygiene, end
    release, per-sentence level): they can't be forwarded chunk by chunk as
    they arrive. ``_tempo`` streams through atempo; base models stream raw."""
    return pipeline_variant(model_id) in ("clean_tempo", "clean_tempo_v2")


def pipeline_family(model_id: str | None) -> str | None:
    """Upstream base model of a pipeline-family id (``eleven_v3_conversational``,
    ``eleven_v4_turbo``, ``eleven_v4``, ``eleven_flash_v2_5`` for its suffixed
    variants), or None for any other model."""
    split = _split_pipeline_model(model_id)
    return split[0] if split else None


def pipeline_variant(model_id: str | None) -> str | None:
    """Pipeline variant of a pipeline-family id: "base", "tempo",
    "clean_tempo", "clean_tempo_v2" — or None when the model has no local
    pipeline at all (plain eleven_v3 / flash / v2)."""
    split = _split_pipeline_model(model_id)
    return split[1] if split else None


def normalize_pipeline_model(model_id: str | None) -> str | None:
    """Map a DragonTTS variant name to the real upstream model id (suffixes
    stripped); ids without a local pipeline pass through unchanged."""
    return pipeline_family(model_id) or model_id


# Path appended to the WS host. The host is derived from the provider's
# base_url (https->wss) so the Indian-residency instance streams against
# wss://api.in.residency.elevenlabs.io rather than the global endpoint.
_ELEVENLABS_WS_PATH = "/v1/text-to-speech/{voice_id}/multi-stream-input"

# Eleven v3: Text-to-Dialogue multi-context socket. The voice is NOT in the
# URL (voices register per context), so a v3 pool is shared across voices by
# the provider's keying — but we keep the same per-voice keying for symmetry.
_ELEVENLABS_TTD_WS_PATH = "/v1/text-to-dialogue/multi-stream-input"

# TTD closes the socket after 20s without a client message, and a keepalive
# must NAME a registered context. This context is registered right after
# connect and never receives text (mirrors pipecat's dialogue service).
_KEEPALIVE_CONTEXT_ID = "dragontts-keepalive"
_KEEPALIVE_INTERVAL = 10.0

# A connection that lives at least this long counts as healthy and resets the
# reconnect backoff; anything shorter (server policy-closes it right after the
# keepalive voice registration) grows the backoff instead, so a rejected voice
# or key retries slowly rather than spinning at ~1.5s forever.
_HEALTHY_CONNECTION_SECS = 15.0

# A connector returns an async context manager whose value is a websocket-like
# object (``await ws.send(str)``, ``async for msg in ws`` -> str, ``await ws.close()``).
ConnectFn = Callable[[str, dict], Any]

# Per-context queue sentinels.
_DONE = object()

# Fire-and-forget socket closes (refresh / reclaim) — held here so the event
# loop's weak reference isn't the only one keeping them alive.
_CLOSING: set[asyncio.Task] = set()


def _close_in_background(conn: _ElevenLabsConnection) -> None:
    task = asyncio.create_task(conn.stop())
    _CLOSING.add(task)
    task.add_done_callback(_CLOSING.discard)


def _reserve(conn: _ElevenLabsConnection) -> _ElevenLabsConnection:
    """Take one context slot on ``conn`` for a sentence (released in stream)."""
    conn.inflight += 1
    LIVE.add(live.WS_IN_FLIGHT, conn.model, conn.account, 1)
    return conn


class _Err:
    """Carries a ProviderError so a dead connection fails its waiters."""

    __slots__ = ("exc",)

    def __init__(self, exc: Exception):
        self.exc = exc


class FirstAudioTimeout(ProviderError):
    """No audio arrived within the first-chunk window (10 s) after the
    utterance was sent. Not a dropped socket — the server just didn't speak —
    so a retry would only add another 10 s of dead air to the call."""


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

# Account rotation: a socket must have sat unused this long before another
# pool may close it to reuse its account slot (see AccountBudget.reclaim), so
# two busy pools don't keep trading the same socket back and forth.
_RECLAIM_MIN_IDLE_SECS = 2.0

# Socket refresh (ELEVENLABS_TTD_WS_REFRESH): a retired socket normally closes
# as soon as its in-flight sentences finish (each is bounded by the stream's
# own first-audio / idle timeouts); this ceiling only catches a context that
# somehow never released its slot. Its sentence then fails over to the
# one-shot retry like any dropped socket.
_REFRESH_MAX_DRAIN_SECS = 60.0
# A replacement that hasn't connected within this long is dropped and the old
# socket keeps serving (tried again at the next interval) — a refresh must
# never take away a working socket for one that can't connect.
_REFRESH_CONNECT_GRACE_SECS = 10.0

# A socket on which a sentence got NO first audio AND nothing at all arrived
# since it was sent looks half-open (dropped without a close frame — sends
# still succeed into the buffer). It is skipped for this long — enough for the
# websockets ping (~20-40 s) to notice a dead link and reconnect — so new
# sentences land on other / fresh sockets instead of dying there one by one.
_SUSPECT_SOCKET_SECS = 30.0

# How long a sentence waits for its FIRST audio chunk before giving up
# (FirstAudioTimeout); after that, the idle timeout ends the stream.
_FIRST_AUDIO_TIMEOUT_SECS = 10.0


class _ElevenLabsConnection:
    """One persistent ElevenLabs socket + its receive/reconnect loop.

    For Text-to-Dialogue (v3) sockets, ``keepalive_voice`` is set: the
    connection registers a dedicated keepalive context immediately after
    connect and pings it every ``keepalive_interval`` seconds so the warm
    socket survives TTD's 20-second client-silence auto-close.
    """

    def __init__(
        self,
        uri: str,
        headers: dict,
        connect_fn: ConnectFn,
        keepalive_voice: str | None = None,
        keepalive_interval: float = _KEEPALIVE_INTERVAL,
        account: str | None = None,
        model: str = "",
    ):
        self._uri = uri
        self.model = model  # live metrics label
        # Account rotation only: the account this socket's key belongs to
        # (None = the residency key), and when it last finished an utterance.
        self.account = account
        self.last_used = time.monotonic()
        self._headers = headers
        self._connect_fn = connect_fn
        self._keepalive_voice = keepalive_voice
        self._keepalive_interval = keepalive_interval
        self.ws: Any = None
        self.ready = asyncio.Event()
        self.contexts: dict[str, asyncio.Queue] = {}
        self.inflight = 0
        # The keepalive context occupies one of the connection's server-side
        # context slots (probe-confirmed: "ka" + 4 user contexts = the cap of
        # 5; a 5th user context is rejected with too_many_contexts), so a TTD
        # connection offers one fewer usable slot than the v2 socket.
        self.max_contexts = (
            _MAX_CONTEXTS_PER_SOCKET - 1
            if keepalive_voice
            else _MAX_CONTEXTS_PER_SOCKET
        )
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False
        # Socket refresh: when the current connection became ready, and
        # whether the pool has retired it (no new sentences; closes once the
        # in-flight ones finish).
        self.connected_at: float | None = None
        self.retiring = False
        self.retiring_since = 0.0
        self.refresh_deferred = False
        # Refresh: a replacement that failed to connect pushes the next try
        # for this socket out by a full interval.
        self.refresh_retry_at = 0.0
        # Last time ANY message arrived on this socket, and until when it is
        # skipped as possibly half-open (see _SUSPECT_SOCKET_SECS).
        self.last_rx = 0.0
        self.suspect_until = 0.0

    async def run(self) -> None:
        """Connect, receive, and reconnect on drop until ``stop()``.

        The backoff only resets for a connection that stayed healthy for a
        while: a socket the server kills ~immediately (e.g. TTD rejecting the
        keepalive voice with a 1008 policy close) must grow the backoff —
        otherwise a permanently-rejected pool reconnects every ~1.5s forever,
        hammering ElevenLabs and flooding the logs.
        """
        backoff = 0.5
        while not self._closed:
            keepalive_task: asyncio.Task | None = None
            ready_at: float | None = None
            try:
                async with self._connect_fn(self._uri, self._headers) as ws:
                    self.ws = ws
                    if self._keepalive_voice is not None:
                        # Register the keepalive context FIRST — TTD rejects a
                        # keepalive naming an unregistered context.
                        await ws.send(
                            json.dumps(
                                {
                                    "context_id": _KEEPALIVE_CONTEXT_ID,
                                    "voices": [self._keepalive_voice],
                                }
                            )
                        )
                        keepalive_task = asyncio.create_task(self._keepalive_loop(ws))
                    self.ready.set()
                    LIVE.add(live.SOCKETS_OPEN, self.model, self.account, 1)
                    LIVE.count(live.SOCKETS_CONNECTED)
                    ready_at = time.monotonic()
                    self.connected_at = ready_at
                    self.last_used = ready_at
                    logger.info("ElevenLabs stream socket ready")
                    async for message in ws:
                        self._dispatch(message)
                self._mark_all_dead("elevenlabs socket closed")
            except Exception as e:  # reconnect on any connection failure
                # CancelledError is BaseException -> propagates, stopping the task.
                self._mark_all_dead(f"elevenlabs socket error: {e}")
            finally:
                if keepalive_task is not None:
                    keepalive_task.cancel()
                self.ready.clear()
                self.ws = None
                if ready_at is not None:
                    LIVE.add(live.SOCKETS_OPEN, self.model, self.account, -1)
                    if not self._closed and not self.retiring:
                        LIVE.count(live.SOCKETS_DROPPED)
                elif not self._closed:
                    LIVE.count(live.CONNECT_FAILURES)
                if (
                    ready_at is not None
                    and time.monotonic() - ready_at >= _HEALTHY_CONNECTION_SECS
                ):
                    backoff = 0.5  # lived long enough — treat as healthy
                else:
                    backoff = min(max(backoff * 2, 1.0), 30.0)

            if self._closed or self.retiring:
                # A retired socket (refresh) never reconnects: it carries no
                # new sentences, and the pool closes it on its next pass.
                break
            logger.debug(f"ElevenLabs socket dropped; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            if self._closed or self.retiring:
                break  # retired / closed during the backoff: don't reconnect

    async def _keepalive_loop(self, ws: Any) -> None:
        """Ping the registered keepalive context to reset the server's
        20-second inactivity timer (no synthesis side effects)."""
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                await ws.send(
                    json.dumps(
                        {"context_id": _KEEPALIVE_CONTEXT_ID, "keep_alive": True}
                    )
                )
        except Exception as e:
            # The receive loop owns reconnection; a failing keepalive just means
            # the socket is going away.
            logger.debug(f"ElevenLabs keepalive stopped: {e}")

    def _dispatch(self, message: str) -> None:
        """Route one ElevenLabs message to its context's queue."""
        self.last_rx = time.monotonic()
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
        if (
            m.get("isFinal")
            or m.get("is_final")
            # TTD-only marker: the last audio chunk of a turn (our usage is one
            # turn per utterance, so it is a reliable end signal there).
            or m.get("is_final_audio_for_turn")
        ):
            q.put_nowait(_DONE)

    def _mark_all_dead(self, reason: str) -> None:
        err = _Err(ProviderError(reason))
        for q in list(self.contexts.values()):
            q.put_nowait(err)

    async def send(self, message: str) -> None:
        """Send one message (serialized per socket)."""
        async with self._send_lock:
            ws = self.ws
            if ws is None:
                raise ProviderError("elevenlabs socket not ready")
            try:
                await ws.send(message)
            except ConnectionClosed:
                # The socket is gone but the receive loop may not have seen it
                # yet; stop handing it out NOW so a retry lands elsewhere. The
                # loop still ends on the closed socket, reconnects and sets
                # ready again. Only if it's still the current connection — a
                # reconnect that already happened must stay ready.
                if self.ws is ws:
                    self.ready.clear()
                raise

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
        # SSML parsing is a CONNECT-time setting (per pipecat/ElevenLabs): a
        # socket parses <break/> tags for ALL utterances multiplexed over it, so
        # SSML-on and SSML-off need separate warm sockets (the provider keys the
        # pool by this flag). Appended to the connect URI as a query param.
        enable_ssml_parsing: bool = False,
        # language_code is a connect-time query param (like model_id/ssml), so
        # the pool is also keyed by it (see provider._get_pool). None = omit.
        language: str | None = None,
        # v3 only: how often to ping the registered keepalive context (TTD
        # auto-closes after 20s of client silence). Exposed for tests.
        keepalive_interval: float = _KEEPALIVE_INTERVAL,
        # Account rotation (TTD only): every socket opens on an account the
        # budget hands out, with that account's key and host; api_key /
        # base_url / min_size / max_size are then unused. None = today's
        # single-key pool, unchanged.
        accounts: AccountBudget | None = None,
        # Socket refresh (TTD only): seconds a socket stays connected before
        # it is replaced — a same-config replacement opens first (when the
        # cap allows), the old one takes no new sentences and closes once
        # its in-flight ones finish. None = never refreshed (today).
        refresh_interval: float | None = None,
    ):
        if not api_key and accounts is None:
            raise ValueError("ElevenLabsStreamPool requires an api_key")
        if not voice_id:
            raise ValueError("ElevenLabsStreamPool requires a voice_id")
        if not model_id:
            raise ValueError("ElevenLabsStreamPool requires a model_id")
        self._api_key = api_key
        self._voice_id = voice_id
        self._language = language
        self._model_id = model_id
        # output_format + model_id are connect-time query params (the socket's
        # format/model is fixed for its lifetime). auto_mode=true lowers latency
        # for complete phrases; inactivity_timeout bumped so warm sockets survive
        # idle gaps between bursts of misses.
        # WS host mirrors the HTTP base_url host (https->wss), so a residency
        # instance streams against the India endpoint, not the global one.
        # TLS only: the xi-api-key header rides the WS handshake, so a plain
        # http:// base_url would put the account key on the wire in cleartext.
        if not base_url.startswith("https://"):
            raise ValueError(
                "ElevenLabsStreamPool requires an https:// base_url (the API key "
                "is sent in the WebSocket handshake)"
            )
        ws_host = base_url.replace("https://", "wss://", 1).rstrip("/")
        self._ttd = is_elevenlabs_ttd_model(model_id)
        if self._ttd:
            # Text-to-Dialogue socket: voice registers per context (not in the
            # URL), auto_mode/inactivity_timeout are not TTD params, and
            # sync_alignment=true matches pipecat's dialogue service. Idle
            # keepalive is handled per connection (see _ElevenLabsConnection).
            self._uri = (
                f"{ws_host}{_ELEVENLABS_TTD_WS_PATH}"
                f"?model_id={quote(model_id, safe='')}"
                f"&output_format={quote(output_format, safe='')}"
                f"&sync_alignment=true"
            )
            if language:
                # v3 models accept language_code (70+ languages); base subtag
                # matches how pipecat resolves codes for the dialogue service.
                self._uri += f"&language_code={quote(language, safe='')}"
        else:
            self._uri = (
                f"{ws_host}{_ELEVENLABS_WS_PATH.format(voice_id=voice_id)}"
                f"?model_id={quote(model_id, safe='')}"
                f"&output_format={quote(output_format, safe='')}"
                f"&auto_mode=true&inactivity_timeout=120"
            )
            if enable_ssml_parsing:
                # Connection-level: the socket parses SSML tags in every utterance
                # sent over it (matches pipecat's connect-URI query param).
                self._uri += "&enable_ssml_parsing=true"
            if language and model_id in _ELEVENLABS_MULTILINGUAL_MODELS:
                # Multilingual models (flash_v2_5/turbo_v2_5) take a language_code;
                # others auto-detect or ignore it. Connect-time, so pool-keyed.
                self._uri += f"&language_code={quote(language, safe='')}"
        self._headers = {"xi-api-key": api_key}
        self._connect_fn = connect_fn or _default_connect
        self._accounts = accounts
        if accounts is not None:
            if not self._ttd:
                raise ValueError(
                    "account rotation applies to Text-to-Dialogue pools only"
                )
            # Same path + query on every account; only host and key differ.
            path_query = self._uri[len(ws_host) :]
            self._account_uris = {
                a.name: a.base_url.replace("https://", "wss://", 1) + path_query
                for a in accounts.accounts
            }
            self._account_headers = {
                a.name: {"xi-api-key": a.api_key} for a in accounts.accounts
            }
            names = list(self._account_uris)
            # Requests currently waiting for a socket on each account, and a
            # circuit breaker per account so a broken account can't stall the
            # other one's share of the traffic.
            self._waiting = dict.fromkeys(names, 0)
            self._account_failures = dict.fromkeys(names, 0)
            self._account_cooldown_until = dict.fromkeys(names, 0.0)
            accounts.register(self)
        self._min_size = min_size
        self._max_size = max_size
        self._conns: list[_ElevenLabsConnection] = []
        self._lock = asyncio.Lock()
        self._started = False
        self._acquire_timeout = acquire_timeout
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._idle_timeout = idle_timeout
        # Parsed from output_format ("pcm_8000" -> 8000) so the idle-timeout
        # warning can report the clip's duration and whether its byte count
        # lands on the model's frame grid (see _end_state).
        tail = output_format.rsplit("_", 1)[-1]
        self._sample_rate = (
            int(tail) if output_format.startswith("pcm_") and tail.isdigit() else 0
        )
        self._keepalive_interval = keepalive_interval
        self._acquire_failures = 0
        self._cooldown_until = 0.0
        self._refresh_interval = refresh_interval if self._ttd else None
        self._refresh_task: asyncio.Task | None = None
        # (old, replacement, since): a replacement still connecting.
        self._refresh_pending: (
            tuple[_ElevenLabsConnection, _ElevenLabsConnection, float] | None
        ) = None

    def _end_state(self, audio_bytes: int) -> str:
        """Duration + frame-grid verdict for an utterance's accumulated bytes.

        A COMPLETE generation is a whole number of the model's audio frames, so
        its byte total lands on the 0.04s grid. A total that does NOT is a
        prefix of the generation — positive evidence the stream was cut rather
        than finished. Returns "" when the rate is unknown (non-pcm format).
        """
        rate = self._sample_rate
        if not rate or not audio_bytes:
            return ""
        frame = rate * 2 // 25  # bytes per 0.04s frame, s16le mono
        aligned = frame and audio_bytes % frame == 0
        verdict = "frame-aligned" if aligned else "OFF-GRID (cut mid-generation)"
        return f" {audio_bytes / 2 / rate:.2f}s {verdict}"

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @property
    def rotates_accounts(self) -> bool:
        """True when sockets come from the account-rotation budget."""
        return self._accounts is not None

    async def start(self) -> None:
        if self._started or self._accounts is not None:
            # Under rotation sockets open on demand from the account budget
            # (a warm socket on the residency key would sit outside it).
            return
        self._started = True
        for _ in range(self._min_size):
            await self._add_connection()
        logger.info(
            f"ElevenLabs stream pool warming {self._min_size} socket(s) for voice={self._voice_id}"
        )

    async def _add_connection(self, account: str | None = None) -> None:
        conn = _ElevenLabsConnection(
            self._account_uris[account] if account else self._uri,
            self._account_headers[account] if account else self._headers,
            self._connect_fn,
            keepalive_voice=self._voice_id if self._ttd else None,
            keepalive_interval=self._keepalive_interval,
            account=account,
            model=self._model_id,
        )
        conn._task = asyncio.create_task(conn.run())
        self._conns.append(conn)
        if self._refresh_interval and self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    def live_counts(self) -> dict[str, dict[str, int]]:
        """Socket states and context slots for the live metrics endpoint."""
        now = time.monotonic()
        sockets = dict.fromkeys(
            ("connecting", "reconnecting", "busy", "idle", "retiring", "suspect"), 0
        )
        total = used = 0
        for c in self._conns:
            if c.retiring:
                sockets["retiring"] += 1
            elif not c.ready.is_set():
                key = "connecting" if c.connected_at is None else "reconnecting"
                sockets[key] += 1
            elif c.suspect_until > now:
                sockets["suspect"] += 1
            else:
                sockets["busy" if c.inflight else "idle"] += 1
                total += c.max_contexts
                used += c.inflight
        return {"sockets": sockets, "slots": {"total": total, "used": used}}

    def _available(self) -> list[_ElevenLabsConnection]:
        """Ready sockets with a free context slot (under the per-connection
        context cap: 5 on the v2 socket, 4 usable on TTD where the keepalive
        context occupies one slot). Sockets retired by the refresh or
        suspected half-open are skipped."""
        now = time.monotonic()
        return [
            c
            for c in self._conns
            if c.ready.is_set()
            and c.inflight < c.max_contexts
            and not c.retiring
            and c.suspect_until <= now
        ]

    async def _refresh_loop(self) -> None:
        """ELEVENLABS_TTD_WS_REFRESH: replace sockets older than the interval."""
        interval = self._refresh_interval
        assert interval
        tick = min(1.0, interval / 4)
        while True:
            await asyncio.sleep(tick)
            try:
                await self._refresh_step()
            except Exception as e:  # never let the loop die
                logger.warning(f"ElevenLabs TTD socket refresh step failed: {e}")

    async def _refresh_step(self) -> None:
        """One pass of the socket refresh. Per pool, one socket at a time:

        1. retire the oldest socket past the interval — make-before-break: a
           replacement (same key, host, model, language, rate) opens first and
           the old socket keeps serving until it is READY; only then does the
           old one stop taking sentences. The replacement opens only if the
           pool's max_size — or, under rotation, the account's budget — has
           room; otherwise the refresh waits (the old socket keeps serving
           normally) until there is. Never drain-first: that would leave the
           pool short a socket mid-traffic. So a refresh can neither push past
           an ElevenLabs limit nor take capacity away;
        2. a retired socket closes once its in-flight sentences finish;
        3. a replacement that can't connect is dropped and the old socket
           keeps serving.
        """
        interval = self._refresh_interval
        assert interval
        now = time.monotonic()
        async with self._lock:
            self._close_drained(now)
            if self._refresh_pending is not None:
                old, new, since = self._refresh_pending
                if old not in self._conns or new not in self._conns:
                    self._refresh_pending = None  # reclaimed / closed meanwhile
                elif new.ready.is_set():
                    self._refresh_pending = None
                    self._retire(old, now, "replacement ready")
                elif now - since >= _REFRESH_CONNECT_GRACE_SECS:
                    self._refresh_pending = None
                    self._drop(new)
                    old.refresh_retry_at = now + interval
                    logger.warning(
                        f"ElevenLabs TTD socket refresh: the replacement didn't "
                        f"connect in {_REFRESH_CONNECT_GRACE_SECS:.0f}s — dropped "
                        f"it, the old socket keeps serving; next try in "
                        f"{interval:.0f}s{self._where(old)}"
                    )
                return
            if any(c.retiring for c in self._conns):
                return
            aged = [
                c
                for c in self._conns
                if c.ready.is_set()
                and not c.retiring
                and c.connected_at is not None
                and now - c.connected_at >= interval
                and now >= c.refresh_retry_at
            ]
            if not aged:
                return
            # Oldest first, but a full account mustn't hold up the others:
            # the first due socket that can get a replacement goes.
            aged.sort(key=lambda c: c.connected_at or now)
            for old in aged:
                if self._room_for_replacement(old):
                    await self._add_connection(old.account)
                    self._refresh_pending = (old, self._conns[-1], now)
                    logger.info(
                        f"ElevenLabs TTD socket refresh: socket connected "
                        f"{now - (old.connected_at or now):.0f}s ago — opening "
                        f"its replacement{self._where(old)}"
                    )
                    return
            # Every due socket is at its cap. An IDLE one can still be swapped
            # when the pool has spare slots elsewhere: close it now, and the
            # next demand reopens a fresh socket in its place. Otherwise wait.
            for old in aged:
                if old.inflight == 0 and self._spare_slots(old) > 0:
                    self._drop(old)
                    logger.info(
                        f"ElevenLabs TTD socket refresh: at the socket cap — "
                        f"closed an idle socket connected "
                        f"{now - (old.connected_at or now):.0f}s ago, a fresh "
                        f"one opens on demand{self._where(old)}"
                    )
                    return
            for old in aged:
                if not old.refresh_deferred:
                    old.refresh_deferred = True
                    logger.info(
                        f"ElevenLabs TTD socket refresh: due, but the socket cap "
                        f"is full and it's busy — it keeps serving until there "
                        f"is room{self._where(old)}"
                    )

    def _room_for_replacement(self, old: _ElevenLabsConnection) -> bool:
        """Claim room for ``old``'s replacement within the cap: the account's
        budget (taking an idle slot from another pool if needed) under
        rotation, the pool's max_size otherwise."""
        if self._accounts is not None and old.account:
            return self._accounts.take(old.account) or self._reclaim(old.account)
        return len(self._conns) < self._max_size

    def _spare_slots(self, old: _ElevenLabsConnection) -> int:
        """Free context slots on the pool's OTHER live sockets that could
        carry ``old``'s traffic (same account under rotation)."""
        return sum(
            c.max_contexts - c.inflight
            for c in self._conns
            if c is not old
            and c.ready.is_set()
            and not c.retiring
            and c.account == old.account
        )

    def _retire(self, conn: _ElevenLabsConnection, now: float, why: str) -> None:
        conn.retiring = True
        conn.retiring_since = now
        LIVE.count(live.SOCKETS_REFRESHED)
        logger.info(
            f"ElevenLabs TTD socket refresh: retiring the old socket ({why})"
            f"{self._where(conn)}"
        )

    def _close_drained(self, now: float) -> None:
        for conn in [c for c in self._conns if c.retiring]:
            drained = conn.inflight == 0
            if drained or now - conn.retiring_since >= _REFRESH_MAX_DRAIN_SECS:
                if not drained:
                    # stop() cancels the receive loop, which would leave these
                    # sentences waiting for audio that never comes; fail them
                    # now so the one-shot retry speaks them on another socket.
                    conn._mark_all_dead("elevenlabs socket closed by refresh")
                self._drop(conn)
                if drained:
                    logger.info(
                        f"ElevenLabs TTD socket refresh: old socket drained and "
                        f"closed{self._where(conn)}"
                    )
                else:
                    logger.warning(
                        f"ElevenLabs TTD socket refresh: closed the old socket "
                        f"with {conn.inflight} sentence(s) still on it after "
                        f"{_REFRESH_MAX_DRAIN_SECS:.0f}s{self._where(conn)}"
                    )

    def _drop(self, conn: _ElevenLabsConnection) -> None:
        """Remove a socket from the pool, free its account slot, close it."""
        self._conns.remove(conn)
        conn.ready.clear()
        if self._accounts is not None and conn.account:
            self._accounts.give(conn.account)
        _close_in_background(conn)

    def _where(self, conn: _ElevenLabsConnection) -> str:
        account = f" account={conn.account}" if conn.account else ""
        return f" [{self._model_id} language={self._language}{account}]"

    async def acquire(self) -> _ElevenLabsConnection:
        try:
            return await self._acquire()
        except SocketUnavailable:
            LIVE.count(live.SOCKET_UNAVAILABLE)
            raise

    async def _acquire(self) -> _ElevenLabsConnection:
        """Return the least-loaded ready socket with a free context slot,
        opening a new one up to ``max_size`` if all are saturated/cold.

        Reserves the context slot (``inflight++``) under the lock so concurrent
        acquires can't all pick the same socket and push it past the hard
        5-context cap (the server would reject the overflow context).
        """
        if self._accounts is not None:
            return await self._acquire_on_account(self._accounts.pick())
        async with self._lock:
            avail = self._available()
            if not avail and len(self._conns) < self._max_size:
                await self._add_connection()
                avail = self._available()
            if avail:
                self._acquire_failures = 0
                conn = min(avail, key=lambda c: c.inflight)
                return _reserve(conn)  # the slot is taken NOW (under the lock)

        if time.monotonic() < self._cooldown_until:
            raise SocketUnavailable("elevenlabs WS unavailable (circuit open)")

        deadline = time.monotonic() + self._acquire_timeout
        while time.monotonic() < deadline:
            async with self._lock:
                avail = self._available()
                if avail:
                    self._acquire_failures = 0
                    conn = min(avail, key=lambda c: c.inflight)
                    return _reserve(conn)
            await asyncio.sleep(0.05)

        self._acquire_failures += 1
        if self._acquire_failures >= self._failure_threshold:
            self._cooldown_until = time.monotonic() + self._cooldown
            logger.warning(
                f"ElevenLabs WS unreachable after {self._acquire_failures} attempt(s) "
                f"— streaming misses fall back to one-shot HTTP synth for {self._cooldown:.0f}s"
            )
        raise SocketUnavailable("no warm elevenlabs socket within acquire timeout")

    async def _acquire_on_account(
        self, account: ElevenLabsAccount
    ) -> _ElevenLabsConnection:
        """Account rotation: a free slot on a socket of ``account`` — never
        another account's (the traffic split is strict).

        A new socket opens only while the account has budget left AND the
        requests waiting on it outnumber the slots of its sockets that are
        still connecting, so a burst opens what it needs (4 requests per
        socket), not one socket per request. With the budget spent, an idle
        socket another pool holds on the account is closed to make room.
        """
        budget = self._accounts
        assert budget is not None
        name = account.name
        if time.monotonic() < self._account_cooldown_until[name]:
            raise SocketUnavailable(
                f"elevenlabs WS unavailable on account {name} (circuit open)"
            )
        deadline = time.monotonic() + self._acquire_timeout
        self._waiting[name] += 1
        try:
            while True:
                async with self._lock:
                    avail = [c for c in self._available() if c.account == name]
                    if avail:
                        self._account_failures[name] = 0
                        conn = min(avail, key=lambda c: c.inflight)
                        return _reserve(conn)
                    # Sockets still on their FIRST connect — their slots are
                    # about to open. One reconnecting after a drop is not
                    # counted: its backoff can run to 30 s, and counting it
                    # would stop waiters from opening a fresh socket while the
                    # account still has budget (they'd time out, then trip
                    # the account's circuit).
                    connecting = sum(
                        1
                        for c in self._conns
                        if c.account == name
                        and c.connected_at is None
                        and not c.retiring
                    )
                    if self._waiting[name] > connecting * (
                        _MAX_CONTEXTS_PER_SOCKET - 1
                    ) and (budget.take(name) or self._reclaim(name)):
                        await self._add_connection(name)
                        logger.info(
                            f"ElevenLabs TTD socket opening on account={name} "
                            f"[{self._model_id} language={self._language}] — "
                            f"sockets/worker {budget.summary()}"
                        )
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.05)
        finally:
            self._waiting[name] -= 1

        self._account_failures[name] += 1
        if self._account_failures[name] >= self._failure_threshold:
            self._account_cooldown_until[name] = time.monotonic() + self._cooldown
            logger.warning(
                f"ElevenLabs WS: no socket on account {name} after "
                f"{self._account_failures[name]} attempt(s) — its share of "
                f"misses fails fast for {self._cooldown:.0f}s "
                f"(sockets/worker {budget.summary()})"
            )
        raise SocketUnavailable(
            f"no elevenlabs socket on account {name} within acquire timeout"
        )

    def _reclaim(self, name: str) -> bool:
        """Move one of ``name``'s slots here: first from a dropped socket
        (here or in another pool) that is waiting out its reconnect backoff,
        then from a live idle socket in another pool."""
        budget = self._accounts
        assert budget is not None
        freed = self._release_dropped_socket(name) or budget.reclaim(name, self)
        return freed and budget.take(name)

    def _release_dropped_socket(self, name: str) -> bool:
        """Close one socket on ``name`` that DROPPED and is waiting to
        reconnect (backoff up to 30 s), returning its slot. Holding the slot
        while it can't carry anything would starve the account under
        repeated drops; a fresh socket connects right away instead."""
        for conn in self._conns:
            if (
                conn.account == name
                and conn.connected_at is not None  # has connected before
                and not conn.ready.is_set()  # ...and is down now
                and conn.inflight == 0
                and not conn.retiring
            ):
                self._drop(conn)
                logger.info(
                    f"ElevenLabs TTD closing a dropped socket waiting to "
                    f"reconnect on account={name}{self._where(conn)} — its slot "
                    f"opens a fresh socket now"
                )
                return True
        return False

    def _release_idle_socket(self, name: str) -> bool:
        """For ANOTHER pool's reclaim: free one slot on ``name`` from a
        dropped socket waiting to reconnect, else from a live idle socket.
        Synchronous (no await), so no acquire can reserve the socket between
        the check and its removal."""
        if self._accounts is None:
            return False
        if self._release_dropped_socket(name):
            return True
        now = time.monotonic()
        for conn in self._conns:
            if (
                conn.account == name
                and conn.ready.is_set()
                and conn.inflight == 0
                and now - conn.last_used >= _RECLAIM_MIN_IDLE_SECS
            ):
                self._drop(conn)
                logger.info(
                    f"ElevenLabs TTD closing an idle socket on account={name} "
                    f"[{self._model_id} language={self._language}] so another pool "
                    f"can use the slot"
                )
                return True
        return False

    async def stream(self, msg: dict) -> AsyncGenerator[bytes, None]:
        """Send one complete utterance over a warm socket and yield its audio.

        ``msg`` carries ``text`` + ``voice_settings`` (no context_id), and on
        the TTD socket optionally ``voice_id`` — the voice registers per
        context there, so one socket can speak any voice (default: the pool's
        own voice). The pool
        injects a unique ``context_id`` and runs the multi-context sequence for
        the socket's endpoint — voices-registration + ``inputs`` for the v3
        Text-to-Dialogue socket, bare-space init + ``text`` for the v2
        text-to-speech socket — then ``flush`` (mirrors pipecat's
        ``ElevenLabsTTSService`` / ``ElevenLabsDialogueTTSService``). Audio
        chunks (base64 ``audio``) are routed back via a per-context queue.
        ElevenLabs does NOT emit ``is_final`` promptly — it parks the context
        ~20s (or ``inactivity_timeout``) past the last audio chunk waiting for
        more text (confirmed by a raw-frame probe), so we end the stream on a
        short idle gap after audio starts; ``is_final`` (and, on the TTD
        socket, ``is_final_audio_for_turn``) still ends us early if it arrives.
        On completion (or early cancellation) we send ``close_context`` so
        ElevenLabs frees the server-side context; the socket stays warm for others.
        """
        conn = (
            await self.acquire()
        )  # reserves a context slot (inflight++) under the lock
        ctx_id = uuid.uuid4().hex
        voice_id = msg.get("voice_id") or self._voice_id
        q: asyncio.Queue = asyncio.Queue()
        conn.contexts[ctx_id] = q
        sent_at = time.monotonic()  # to tell a half-open socket (see below)
        try:
            if self._ttd:
                # Text-to-Dialogue sequence (mirrors pipecat's
                # ElevenLabsDialogueTTSService): 1) open the context with a
                # voices registration (only stability is honored in
                # voice_settings), 2) send the utterance as ONE input starting
                # a new turn, 3) flush.
                init: dict = {"context_id": ctx_id, "voices": [voice_id]}
                if msg.get("voice_settings"):
                    init["voice_settings"] = msg["voice_settings"]
                await conn.send(json.dumps(init))
                await conn.send(
                    json.dumps(
                        {
                            "context_id": ctx_id,
                            "inputs": [
                                {
                                    "text": msg.get("text", ""),
                                    "voice_id": voice_id,
                                    "new_turn": True,
                                }
                            ],
                        }
                    )
                )
            else:
                # pipecat's multi-stream-input sequence: 1) init the context
                # with a bare space + voice_settings, 2) send the real text.
                init = {"text": " ", "context_id": ctx_id}
                if msg.get("voice_settings"):
                    init["voice_settings"] = msg["voice_settings"]
                await conn.send(json.dumps(init))
                await conn.send(
                    json.dumps({"text": msg.get("text", ""), "context_id": ctx_id})
                )
            await conn.send(json.dumps({"context_id": ctx_id, "flush": True}))
            # ElevenLabs does NOT emit is_final promptly — it parks the context
            # ~20s (or inactivity_timeout) after the last audio chunk waiting for
            # more text (probe + pipecat's own comment confirm this). So we end
            # the stream on a short idle gap AFTER audio starts; is_final still
            # ends us early if it happens to arrive. The first frame gets a
            # longer timeout so a slow cold-start doesn't false-positive.
            got_audio = False
            chunks = 0
            audio_bytes = 0
            while True:
                timeout = self._idle_timeout if got_audio else _FIRST_AUDIO_TIMEOUT_SECS
                try:
                    item = await asyncio.wait_for(q.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if not got_audio:
                        if conn.last_rx < sent_at:
                            # Not a single message on this socket since the
                            # sentence went out: likely half-open. Skip it for
                            # a while so new sentences don't die here too.
                            conn.suspect_until = time.monotonic() + _SUSPECT_SOCKET_SECS
                            logger.warning(
                                f"ElevenLabs socket silent since the sentence "
                                f"was sent — possibly half-open, skipped for "
                                f"{_SUSPECT_SOCKET_SECS:.0f}s"
                                f"{self._where(conn)}"
                            )
                        LIVE.count(live.FIRST_AUDIO_TIMEOUTS)
                        raise FirstAudioTimeout(
                            "elevenlabs stream timed out waiting for first audio chunk"
                        )
                    # TRUNCATION RISK — this is a GUESS that the utterance
                    # ended, made because the server went quiet, not because it
                    # said so. A mid-generation stall longer than the idle
                    # window is indistinguishable from a real ending here, and
                    # the partial clip flows on to be served AND cached as if
                    # complete (v3 stores the finished bytes, so a truncated
                    # entry is replayed for its whole TTL). Warn so the guess is
                    # visible; _end_state adds the frame-grid verdict, which is
                    # positive evidence when the clip really was cut.
                    if self._ttd:
                        logger.warning(
                            f"ElevenLabs stream ended on IDLE TIMEOUT "
                            f"({self._idle_timeout:.2f}s, no is_final) — "
                            f"ctx={ctx_id[:8]} voice={voice_id} "
                            f"chunks={chunks} bytes={audio_bytes}"
                            f"{self._end_state(audio_bytes)} — MAY BE TRUNCATED"
                        )
                    else:
                        # Classic (flash / turbo) socket: it never sends is_final
                        # promptly, so silence IS its normal end (live-checked:
                        # every flash stream ends here, each in trailing silence
                        # like the complete HTTP clip). Not a truncation signal,
                        # and the 0.04 s frame grid is a TTD property — no verdict.
                        logger.debug(
                            f"ElevenLabs stream ended on idle "
                            f"({self._idle_timeout:.2f}s; the normal end on this "
                            f"socket) — ctx={ctx_id[:8]} chunks={chunks} "
                            f"bytes={audio_bytes}"
                        )
                    break  # silence after audio => end of utterance
                if item is _DONE:
                    if not got_audio and conn.account is not None:
                        # Account rotation: an account that ends the turn with
                        # NO audio (no TTD access / credits — live-observed on
                        # a scoped key) would otherwise be served as an empty
                        # 200 AND cached as silence for its whole TTL. Fail it
                        # so the one-shot retry re-picks an account and
                        # nothing empty is stored. (Single-key pools keep
                        # today's behavior.)
                        raise ProviderError(
                            f"elevenlabs account {conn.account} ended the turn "
                            f"with no audio"
                        )
                    # Server-declared end (is_final / is_final_audio_for_turn):
                    # the only exit that is known-complete rather than inferred.
                    logger.debug(
                        f"ElevenLabs stream ended on is_final — ctx={ctx_id[:8]} "
                        f"chunks={chunks} bytes={audio_bytes}"
                        f"{self._end_state(audio_bytes)}"
                    )
                    break
                if isinstance(item, _Err):
                    raise item.exc
                got_audio = True
                chunks += 1
                audio_bytes += len(item)
                yield item
        finally:
            conn.contexts.pop(ctx_id, None)
            conn.inflight -= 1
            LIVE.add(live.WS_IN_FLIGHT, conn.model, conn.account, -1)
            conn.last_used = time.monotonic()
            # Free the server-side context (best effort); the socket stays warm.
            try:
                await conn.send(
                    json.dumps({"context_id": ctx_id, "close_context": True})
                )
            except Exception:
                pass

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        for conn in list(self._conns):
            await conn.stop()
            if self._accounts is not None and conn.account:
                self._accounts.give(conn.account)
        if self._accounts is not None:
            self._accounts.unregister(self)
        self._conns.clear()
        self._started = False
