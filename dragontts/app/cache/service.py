"""Cache service — lookup / synth / store-native / convert-on-serve + metrics.

The cache key is format-agnostic (text + provider + voice + model + language +
params). Audio is stored once in the provider's *native* format and converted to
the caller's requested ``output_format`` on serve, so a single entry serves
every format — the one-shot μ-law path and the streaming PCM path share it.

Read path: cache check → HIT loads native, converts to requested, returns
(+ records a hit) → MISS synthesizes native, write-throughs native, converts,
returns (+ records a miss). Streaming MISS forwards native chunks live (when
the requested format == native) and stores native on clean completion.
Admin: check / create / delete / clear. Metrics flow into a daily rollup; the
cache snapshot is served from incrementally-maintained totals.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.audio.format import convert_audio
from app.audio.text import normalize_for_tts, prepend_leading_dot
from app.cache.key import (
    canonical_params,
    hash_key,
    normalize_text,
    parse_model_id,
)
from app.cache.metrics import WriteBehindMetrics
from app.cache.resilience import get_gate
from app.core.config import settings
from app.core.logging import logger
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError
from app.providers.registry import ProviderNotConfigured
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from app.storage.base import CacheRecord, escape_like


def _wc(text: str | None) -> int:
    """Word count of a transcript (whitespace tokens, punctuation kept)."""
    return len((text or "").split())


def _decode_upload_to_pcm(data: bytes) -> tuple[bytes, int]:
    """Decode an uploaded clip to mono PCM s16le + its sample rate.

    Accepts WAV (parsed via stdlib ``wave``); raises ``ValueError`` (which the
    API layer maps to HTTP 400) on anything else — INCLUDING malformed frames
    that make ``audioop`` raise (``audioop.error`` is NOT a ``ValueError``, so
    the tomono/lin2lin calls are inside the try too). MP3/ogg are NOT supported
    (no ffmpeg/pydub). Stereo is down-mixed and non-16-bit widened. Duration is
    capped to bound memory.
    """
    import audioop
    import wave
    from io import BytesIO

    MAX_DURATION_S = 120  # reject absurdly long uploads before allocating PCM
    try:
        with wave.open(BytesIO(data), "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            if (
                framerate <= 0
                or framerate > 96000
                or sampwidth not in (1, 2, 3, 4)
                or nchannels not in (1, 2)
                or nframes / framerate > MAX_DURATION_S
            ):
                raise ValueError(
                    f"unsupported WAV: {nchannels}ch {sampwidth}*{framerate}Hz "
                    f"{nframes} frames"
                )
            frames = wf.readframes(nframes)
        if nchannels > 1:
            frames = audioop.tomono(frames, sampwidth, 1.0, 1.0)
        if sampwidth != 2:
            frames = audioop.lin2lin(frames, sampwidth, 2)
    except ValueError:
        raise
    except Exception as e:  # malformed/not-WAV, OR audioop.error on odd frames
        raise ValueError(f"upload is not a decodable WAV file: {e}") from e
    return frames, framerate


# Sentinel: caller has NOT pre-fetched the existing record (vs. None = checked,
# genuinely absent). Avoids a redundant get() on every store when the caller
# already knows.
_UNCHECKED = object()

# Chunk size used when streaming a complete blob back to the caller (HIT, or a
# one-shot miss). Large enough to amortize per-iteration overhead, small enough
# to start flowing promptly.
_STREAM_CHUNK = 16 * 1024


async def _chunked(
    data: bytes, size: int = _STREAM_CHUNK
) -> AsyncGenerator[bytes, None]:
    """Yield ``data`` in fixed-size byte chunks for an HTTP streaming response."""
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _split_transcript(text: str, symbols: str, min_words: int = 2) -> list[str]:
    """Split ``text`` into sentence chunks at whitespace following a ``symbols`` char.

    Used by the SPLIT_AT_SYMBOLS feature so a multi-sentence transcript (e.g. a
    greeting clairvoyance sends whole) is synthesized + cached one sentence per
    key — recurring sentences then reuse their own entry. Each chunk KEEPS its
    trailing punctuation (the per-part cache key matches a request that sends
    that sentence alone). The split is at whitespace AFTER a symbol, which keeps
    decimals ("3.14"), IPs ("192.168.1.1") and URLs ("example.com/path") intact
    — the dot there isn't followed by whitespace. Empty/whitespace-only results
    are dropped.

    ``min_words`` is a PER-PART gate: a part with fewer words is too small to be
    worth its own cache entry, so consecutive short parts are merged into one —
    but a long sentence is NEVER merged with a short neighbour. So a single short
    sentence no longer collapses the WHOLE phrase to one entry: "thanks. OK. that
    will be all" => ["thanks. OK.", "that will be all"] (the long sentence keeps
    its own key) instead of one merged chunk.

    Returns ``[text]`` (one chunk) when ``symbols`` is empty, no split point
    exists, or every part ended up merged back into one — so a caller's
    ``len(parts) > 1`` check is the on/off gate.

    Note: abbreviations like "Mr. Smith" WILL split (no dictionary) — acceptable
    for the scripted-greeting use case; set ``symbols`` to "." only to minimize it.
    """
    if not symbols or not text or not text.strip():
        return [text] if (text and text.strip()) else []
    parts = [
        p.strip()
        for p in re.split(rf"(?<=[{re.escape(symbols)}])\s+", text)
        if p.strip()
    ]
    if len(parts) > 1:
        # Per-part min-words gate: a part with fewer than min_words is too small
        # to be worth its own cache entry, so consecutive short parts are merged
        # into one (and a trailing short run is kept together). A long sentence
        # is never merged with a short neighbour, so one short sentence ("OK.")
        # no longer collapses the whole phrase — the recurring long sentences
        # keep their own keys.
        merged: list[str] = []
        short_run = ""
        for p in parts:
            if len(p.split()) < min_words:
                short_run = f"{short_run} {p}".strip() if short_run else p
            else:
                if short_run:
                    merged.append(short_run)
                    short_run = ""
                merged.append(p)
        if short_run:
            merged.append(short_run)
        parts = merged or [text]
    return parts


# Split parts are pure-concatenated — no edge trim, no inserted gap — so nothing
# is cut and each part's audio is appended exactly as the provider returned it
# (see _get_or_synthesize_split / _stream_split).


def _same_format(encoding_a: str, rate_a: int, encoding_b: str, rate_b: int) -> bool:
    return encoding_a.lower() == encoding_b.lower() and rate_a == rate_b


class CacheService:
    def __init__(
        self,
        metadata,
        blobs,
        get_provider: Callable[[str], BaseTTSProvider | None],
    ):
        self._metadata = metadata
        self._blobs = blobs
        self._get_provider = get_provider
        # Write-behind wrapper for stats-only writes (HIT touch + metrics) so a HIT
        # returns audio without awaiting a SQLite commit. Falls back to the raw
        # store (synchronous) when disabled. Correctness-critical writes
        # (put/put_with_totals/delete/adjust_totals) stay on self._metadata.
        self._metrics = (
            WriteBehindMetrics(
                metadata,
                settings.metrics_flush_interval_ms / 1000.0,
                settings.metrics_flush_batch_size,
            )
            if settings.metrics_write_behind_enabled
            else metadata
        )
        # Ephemeral session counters (reset on restart); durable metrics are in the DB.
        self._hits = 0
        self._misses = 0
        # Per-key in-flight synth futures for single-flight: N concurrent
        # identical MISSes share ONE synth + ONE store. Loop-thread-only (no lock).
        self._inflight: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        """Start background tasks (write-behind metrics flusher)."""
        if hasattr(self._metrics, "start"):
            self._metrics.start()

    async def stop(self) -> None:
        """Flush + stop background tasks (graceful shutdown loses no metrics)."""
        if hasattr(self._metrics, "stop"):
            await self._metrics.stop()

    # -- internals -----------------------------------------------------------

    def _resolve(self, req: TTSRequest):
        # Normalize the transcript once so the cache KEY and the text sent to the
        # provider SYNTH stay in sync (strips LLM zero-width artifacts like the
        # ZWJ in "వెబ్‌సైట్", collapses whitespace, NFC). Without this, the key
        # was normalized but the synth text was raw — they could diverge, and ZWJ
        # reached the provider (audible artifacts).
        req.transcript = normalize_text(req.transcript)
        provider, model = parse_model_id(req.model_id)
        of = req.output_format
        params_canon = canonical_params(provider, req.params)
        # Number-normalize the transcript (Indian grouping) so the cache KEY and
        # the synth-text base stay in sync -- "599" and "5 hundred 99" share one
        # entry. This is KEY text: it is provider-hint-free, so the ElevenLabs
        # leading dot is NOT added here (it's a synth-only hint, applied in
        # _synthesize / _stream_and_store). A dot-free key matches a request
        # that sends the phrase alone, so the entry is reusable.
        req.transcript = normalize_for_tts(req.transcript, provider)
        key = hash_key(
            text=req.transcript,
            provider=provider,
            voice_id=req.voice.id,
            model=model,
            language=req.language,
            params_canonical=params_canon,
        )
        return provider, model, of, params_canon, key

    @staticmethod
    def _expired(record: CacheRecord) -> bool:
        return bool(
            record.ttl_expires_at
            and datetime.fromisoformat(record.ttl_expires_at)
            <= datetime.now(timezone.utc)
        )

    async def _timed(self, kind: str, t0: float, provider: str) -> None:
        """Record a latency sample (sampling-gated) for the avg/p95 rollup, tagged
        with the routed provider for the per-provider latency view."""
        rate = settings.metrics_latency_sample_rate
        if rate > 0 and random.random() < rate:
            await self._metrics.record_latency(
                kind, int((time.perf_counter() - t0) * 1_000_000), provider
            )

    async def _timed_chunks(self, gen, t0: float, provider: str):
        """Wrap a streamed chunk generator so latency rides the stream lifecycle:
        ``ttfb`` at the first yielded byte, ``total`` after the last. stream()
        returns the generator BEFORE the request finishes (the caller iterates
        it later), so end-to-end latency can't be recorded synchronously like
        the one-shot bytes path — it must fire here. ``finally`` covers both
        clean completion and a client disconnect (aclose). Each sample is
        independently sampling-gated via :meth:`_timed`."""
        first = True
        try:
            async for chunk in gen:
                if first:
                    await self._timed("ttfb", t0, provider)
                    first = False
                yield chunk
        finally:
            await self._timed("total", t0, provider)

    async def _synthesize(
        self, req: TTSRequest, provider: str, model: str
    ) -> AudioResult:
        """Synthesize via the routed provider; return its NATIVE-format audio.

        Runs under the provider's resilience gate (bulkhead + rate limit) so a
        slow/hung provider is capped and can't starve the others.
        """
        instance = self._get_provider(provider)
        if instance is None:
            raise ProviderNotConfigured(provider)
        gate = get_gate(provider)
        text = prepend_leading_dot(req.transcript, provider)
        async with gate:
            t0 = time.perf_counter()
            if req.params.get("enable_ssml_parsing"):
                # SSML: synthesize via the streaming (WS) path, not the one-shot
                # HTTP synth. ElevenLabs renders <break/> tags more cleanly over
                # the WS path (the HTTP one-shot can split a word across a break),
                # so the cached audio is the smoother WS rendering. stream_synth
                # falls back to HTTP itself if no warm socket is available.
                chunks = []
                async for chunk in instance.stream_synth(
                    text=text,
                    voice_id=req.voice.id,
                    model=model,
                    language=req.language,
                    params=req.params,
                ):
                    chunks.append(chunk)
                result = AudioResult(
                    audio=b"".join(chunks),
                    container="raw",
                    encoding=instance.native_encoding,
                    sample_rate=instance.native_sample_rate,
                )
            else:
                result = await instance.synth(
                    text=text,
                    voice_id=req.voice.id,
                    model=model,
                    language=req.language,
                    params=req.params,
                )
        await self._timed("synth", t0, provider)
        return result

    @staticmethod
    def _ttl_expires_at(text: str) -> str | None:
        """Length-scaled expiry timestamp (ISO UTC, no microseconds — matches the
        purge/backfill format so SQL ``ttl_expires_at < now`` string comparison
        is correct). ``clamp(BASE + PER_WORD*words, BASE, MAX)``. Applied to
        every stored entry (write-through) — there's no permanent
        tier; the periodic purge job evicts expired rows + blobs. Returns None
        when TTL is disabled (``BASE<=0``) so the row never expires."""
        if settings.cache_ttl_base_seconds <= 0:
            return None
        words = _wc(text)
        ttl = min(
            settings.cache_ttl_base_seconds
            + settings.cache_ttl_per_word_seconds * words,
            settings.cache_ttl_max_seconds,
        )
        return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )

    async def _store(
        self,
        key: str,
        req: TTSRequest,
        provider: str,
        model: str,
        params_canon: str,
        audio: bytes,
        encoding: str,
        sample_rate: int,
        existing=_UNCHECKED,
        replace: bool = False,
    ) -> None:
        """Store ``audio`` (already in ``encoding``/``sample_rate``) as native.

        Fresh misses (no existing row, ``replace=False``) use INSERT OR IGNORE so
        concurrent identical misses can't double-count ``provider_totals``.
        Existing rows (expired refresh) and explicit overrides (``replace=True``)
        REPLACE the row and adjust totals by the size delta.
        """
        if existing is _UNCHECKED:
            existing = await self._metadata.get(key)
        now = datetime.now(timezone.utc)
        storage_path = await self._blobs.put(key, audio)
        ttl = self._ttl_expires_at(req.transcript)
        record = CacheRecord(
            key=key,
            provider=provider,
            voice_id=req.voice.id,
            model=model,
            language=req.language,
            params=params_canon,
            text=req.transcript,
            container="raw",
            encoding=encoding,
            sample_rate=sample_rate,
            size_bytes=len(audio),
            storage_path=storage_path,
            hit_count=0,
            created_at=now.isoformat(),
            last_accessed_at=now.isoformat(),
            ttl_expires_at=ttl,
        )
        if existing is None and not replace:
            # Fresh miss: INSERT OR IGNORE — race-free totals under concurrent
            # identical misses (only the first store inserts + bumps totals).
            await self._metadata.put_with_totals(record)
        else:
            # Existing row (refresh) or explicit override: REPLACE + totals
            # delta in one transaction (put + adjust separately would let a
            # concurrent delete drift provider_totals).
            await self._metadata.replace_with_totals(record)

    async def _convert_audio(
        self,
        data: bytes,
        *,
        native_encoding: str,
        native_rate: int,
        out_encoding: str,
        out_rate: int,
    ) -> bytes:
        """convert_audio off the event loop.

        audioop resample/μ-law conversion is CPU-bound and runs on every serve
        (HIT and MISS); inline it would block the loop (and starve the Cartesia
        socket pumps) under concurrent load, so dispatch it to the worker pool.
        """
        return await asyncio.to_thread(
            convert_audio,
            data,
            native_encoding=native_encoding,
            native_rate=native_rate,
            out_encoding=out_encoding,
            out_rate=out_rate,
        )

    # -- read path -----------------------------------------------------------

    async def _produce(
        self,
        req: TTSRequest,
        provider: str,
        model: str,
        params_canon: str,
        key: str,
        record,
    ) -> tuple[bytes, str, int, str]:
        """Synth + store for a MISS. Returns (native, encoding, rate, status)
        where status is 'MISS', or 'HIT' (if the cache was filled between the
        outer lookup and here — e.g. by a concurrent request)."""
        rec = await self._metadata.get(key)
        if rec and not self._expired(rec):
            return (
                await self._blobs.get(rec.storage_path),
                rec.encoding,
                rec.sample_rate,
                "HIT",
            )
        native = await self._synthesize(req, provider, model)
        if settings.enable_write_through:
            await self._store(
                key,
                req,
                provider,
                model,
                params_canon,
                native.audio,
                native.encoding,
                native.sample_rate,
                existing=record,
            )
        return native.audio, native.encoding, native.sample_rate, "MISS"

    async def _produce_native(
        self,
        req: TTSRequest,
        provider: str,
        model: str,
        params_canon: str,
        key: str,
        record,
    ) -> tuple[bytes, str, int, str, bool, bool]:
        """Single-flight: produce + store native audio for ``key`` exactly once.

        Concurrent MISSes for the same key share one synth + one store (the
        producer's future). Returns (native, encoding, rate, status, produced,
        coalesced):

        - produced=True, coalesced=False: we ran the synth (records a miss +
          synth_call).
        - produced=False, coalesced=True: we awaited an in-flight producer's
          future (records a hit — served without our own synth).
        - produced=False, coalesced=False: we were the producer, but ``_produce``
          short-circuited to a HIT because a concurrent request filled the key
          between our outer MISS lookup and the inner re-fetch (also a hit).
        """
        fut = self._inflight.get(key)
        if fut is not None:
            native, enc, rate, status = await fut
            return native, enc, rate, status, False, True  # coalesced — no own synth
        fut = self._new_inflight(key)
        try:
            result = await self._produce(
                req, provider, model, params_canon, key, record
            )
            if not fut.done():
                fut.set_result(result)
            native, enc, rate, status = result
            # `produced` = a synth actually ran. _produce can short-circuit to
            # status='HIT' (a concurrent request filled the key between the outer
            # MISS lookup and the inner re-fetch) — that served a cache read, not
            # a synth, so it must count as a HIT, not a miss/synth_call.
            produced = status == "MISS"
            return native, enc, rate, status, produced, False  # not coalesced
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            if self._inflight.get(key) is fut:
                self._inflight.pop(key, None)

    def _new_inflight(self, key: str) -> asyncio.Future:
        """Create + register the single-flight future for ``key``.

        A done-callback retrieves any exception so a producer that aborts with no
        coalesced waiter (e.g. a streaming client disconnects, or a provider
        errors mid-stream) doesn't leave an *unretrieved* exception. asyncio logs
        those at GC time ("Future exception was never retrieved"), and that log
        routes through the loguru intercept and deadlocks. A waiter that DOES
        ``await`` the future still raises normally — this callback only marks the
        exception retrieved (idempotent with the waiter's own retrieval).
        """
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(self._consume_inflight_exc)
        self._inflight[key] = fut
        return fut

    @staticmethod
    def _consume_inflight_exc(fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()  # retrieve -> not "Future exception was never retrieved"
        if exc is not None:
            logger.warning(f"in-flight synth aborted (no waiter consumed it): {exc!r}")

    async def get_or_synthesize(self, req: TTSRequest) -> tuple[bytes, dict]:
        # SPLIT_AT_SYMBOLS: when set and the transcript splits into >1 sentence,
        # synth/serve each sentence independently (each caches under its own key).
        # Empty (default) / no split -> this is skipped and the single-phrase path
        # below runs byte-for-byte unchanged.
        if settings.split_at_symbols:
            parts = _split_transcript(
                req.transcript,
                settings.split_at_symbols,
                settings.split_min_words_per_part,
            )
            if len(parts) > 1:
                return await self._get_or_synthesize_split(req, parts)
        provider, model, of, params_canon, key = self._resolve(req)
        t0 = time.perf_counter()

        record = await self._metadata.get(key)
        if record and not self._expired(record):
            t_cs = time.perf_counter()
            native = await self._blobs.get(record.storage_path)
            audio = await self._convert_audio(
                native,
                native_encoding=record.encoding,
                native_rate=record.sample_rate,
                out_encoding=of.encoding,
                out_rate=of.sample_rate,
            )
            await self._metrics.touch_and_record(
                key,
                {
                    "requests": 1,
                    "hits": 1,
                    "bytes_served": len(audio),
                    "words_served": _wc(req.transcript),
                },
                provider=provider,
            )
            self._hits += 1
            logger.info(f"CACHE HIT  key={key[:12]}… provider={provider}")
            await self._timed("cache_serve", t_cs, provider)
            await self._timed("total", t0, provider)
            return audio, {"X-Cache": "HIT", "X-Cache-Key": key}

        logger.info(f"CACHE MISS key={key[:12]}… provider={provider} — synthesizing")

        # Single-flight: N concurrent identical misses share one synth + one store.
        native, nenc, nrate, status, produced, coalesced = await self._produce_native(
            req, provider, model, params_canon, key, record
        )
        audio = await self._convert_audio(
            native,
            native_encoding=nenc,
            native_rate=nrate,
            out_encoding=of.encoding,
            out_rate=of.sample_rate,
        )
        if produced:
            await self._metrics.record_metrics(
                provider=provider,
                requests=1,
                misses=1,
                bytes_served=len(audio),
                synth_calls=1,
                words_served=_wc(req.transcript),
                words_synthesized=_wc(req.transcript),
            )
            self._misses += 1
            logger.info(f"CACHE {status} key={key[:12]}… provider={provider}")
        else:
            # Served from cache without our own synth: either we coalesced onto an
            # in-flight producer, or _produce short-circuited to a HIT (a
            # concurrent request filled the key between our MISS lookup and the
            # inner re-fetch). Both count as a hit.
            await self._metrics.touch_and_record(
                key,
                {
                    "requests": 1,
                    "hits": 1,
                    "bytes_served": len(audio),
                    "words_served": _wc(req.transcript),
                },
                provider=provider,
            )
            self._hits += 1
            status = "HIT"
            kind = "coalesced" if coalesced else "concurrent fill"
            logger.info(f"CACHE HIT ({kind}) key={key[:12]}… provider={provider}")
        await self._timed("total", t0, provider)
        return audio, {"X-Cache": status, "X-Cache-Key": key}

    # -- SPLIT_AT_SYMBOLS (multi-sentence request -> one entry per sentence) --

    async def _is_cached(self, req: TTSRequest) -> bool:
        """True iff ``req`` resolves to a non-expired cache entry.

        Used by the split stream path to set the aggregate X-Cache header up
        front (headers must be returned before the generator streams).
        """
        _provider, _model, _of, _params, key = self._resolve(req)
        record = await self._metadata.get(key)
        return bool(record and not self._expired(record))

    async def _get_or_synthesize_split(
        self,
        req: TTSRequest,
        parts: list[str],
    ) -> tuple[bytes, dict]:
        """Serve a multi-sentence request as one cache entry per sentence.

        Clones ``req`` per part and routes each through :meth:`get_or_synthesize`
        (so each sentence is synthesized/served + cached under its OWN key and is
        reused by any later request that sends that sentence alone), then
        concatenates each part's audio VERBATIM — no edge trim, no inserted gap
        (nothing is cut; the result is synth(part0) + synth(part1) + ...). The
        aggregate X-Cache is HIT only when EVERY part was a HIT, else MISS (at
        least one sentence synthesized). The full-phrase key is reported in
        X-Cache-Key for caller reference — note the full phrase itself is NOT
        cached, only its parts.

        Each part has no whitespace-after-symbol internally (the split consumed
        those), so the recursive :meth:`get_or_synthesize` call hits the
        single-phrase path and does not recurse further. Only reached when
        ``settings.split_at_symbols`` is set AND the transcript splits into >1 part.
        """
        # Full-phrase key for the header. _resolve normalizes req.transcript in
        # place, but `parts` were already captured from the raw transcript by the
        # caller, so mutating req here is harmless.
        _p, _m, of, _pc, full_key = self._resolve(req)
        part_reqs = [req.model_copy(update={"transcript": p}) for p in parts]
        # split_parallel (env, default OFF): synth the parts concurrently instead
        # of one at a time. gather preserves submission order, so the
        # concatenation stays synth(part0) + synth(part1) + ... — identical audio.
        if settings.split_parallel:
            results = await asyncio.gather(
                *(self.get_or_synthesize(pr) for pr in part_reqs)
            )
        else:
            results = [await self.get_or_synthesize(pr) for pr in part_reqs]
        chunks = [audio for audio, _headers in results]
        # Aggregate HIT only when EVERY part was a genuine HIT (any synthed part
        # => MISS), from each part's real status — so it can't lie.
        all_hit = all(headers.get("X-Cache") == "HIT" for _a, headers in results)
        # Pure concatenation: append each part's audio verbatim — no trim, no gap
        # (nothing is cut; the result is synth(part0) + synth(part1) + ...).
        joined = b"".join(c for c in chunks if c)
        return joined, {
            "X-Cache": "HIT" if all_hit else "MISS",
            "X-Cache-Key": full_key,
        }

    async def _stream_split(
        self,
        req: TTSRequest,
        parts: list[str],
    ) -> AsyncGenerator[bytes, None]:
        """Stream a multi-sentence request one sentence at a time, back to back.

        Each part is served via :meth:`get_or_synthesize` (so it caches under its
        own key) and its converted audio chunked out VERBATIM, in order — no edge
        trim, no inserted gap (nothing is cut; parts are appended exactly as
        synthesized). For a MISS part this is synth-then-yield rather than live
        provider streaming — acceptable for short sentences and only when
        SPLIT_AT_SYMBOLS is on; the default (flag off) stream path keeps live
        provider streaming. Each part's audio is already in the requested output
        format (get_or_synthesize converts), so it is emitted unchanged.
        """
        for part in parts:
            part_req = req.model_copy(update={"transcript": part})
            audio, _headers = await self.get_or_synthesize(part_req)
            if not audio:
                continue  # empty part: skip
            async for chunk in _chunked(audio):
                yield chunk

    async def _stream_resolved(
        self,
        results: list[tuple[bytes, dict]],
    ) -> AsyncGenerator[bytes, None]:
        """Stream already-resolved per-part audio in order, back to back.

        Companion to the ``split_parallel`` stream path: each (audio, headers)
        pair was produced by :meth:`get_or_synthesize` (so the audio is already
        in the requested output_format) and is emitted verbatim in part order,
        chunked — no edge trim, no inserted gap (parts are appended exactly as
        synthesized).
        """
        for audio, _headers in results:
            if not audio:
                continue  # empty part: skip
            async for chunk in _chunked(audio):
                yield chunk

    # -- streaming read path ------------------------------------------------

    async def stream(self, req: TTSRequest) -> tuple[dict, AsyncGenerator[bytes, None]]:
        """Resolve ``req`` and return (response headers, audio-chunk generator).

        HIT: native blob loaded, converted to the requested format if needed,
        then streamed in fixed-size chunks.
        MISS (requested == native): provider chunks forwarded live (low TTFB),
        native clip stored on clean completion.
        MISS (requested != native): can't resample per-chunk, so synthesize
        native fully, store native, convert, then chunk.
        """
        t0 = (
            time.perf_counter()
        )  # entry; total = entry -> last byte (via _timed_chunks)
        # SPLIT_AT_SYMBOLS_STREAM: when set and the transcript splits into >1
        # sentence, stream each sentence independently (each caches under its own
        # key). Independent of SPLIT_AT_SYMBOLS (the /tts/bytes knob). Empty
        # (default) / no split -> skipped; the single-phrase stream path below
        # runs byte-for-byte unchanged.
        if settings.split_at_symbols_stream:
            parts = _split_transcript(
                req.transcript,
                settings.split_at_symbols_stream,
                settings.split_min_words_per_part,
            )
            if len(parts) > 1:
                provider, _model, _of, _pc, full_key = self._resolve(req)
                if settings.split_parallel:
                    # Opt-in: serve every part concurrently, THEN derive the
                    # aggregate X-Cache from each part's TRUE status (no stale
                    # metadata probe, so it can't claim HIT when a part's TTL
                    # expired between header and serve). Split-stream is already
                    # synth-then-yield (not live), so resolving up front adds no
                    # latency vs the split path's norm; the first byte arrives
                    # after the slowest part instead of after part0.
                    part_reqs = [
                        req.model_copy(update={"transcript": p}) for p in parts
                    ]
                    results = await asyncio.gather(
                        *(self.get_or_synthesize(pr) for pr in part_reqs)
                    )
                    all_hit = all(h.get("X-Cache") == "HIT" for _a, h in results)
                    return (
                        {
                            "X-Cache": "HIT" if all_hit else "MISS",
                            "X-Cache-Key": full_key,
                        },
                        self._timed_chunks(
                            self._stream_resolved(results), t0, provider
                        ),
                    )
                # Default (split_parallel off): set the header from a cheap
                # metadata probe (HIT only when EVERY part is cached — any partial
                # => MISS), then stream each part in order. The probe can be stale
                # on a TOCTOU (a part expires between probe and serve); opt into
                # split_parallel above for a truthful header.
                cached = [
                    await self._is_cached(req.model_copy(update={"transcript": p}))
                    for p in parts
                ]
                return (
                    {
                        "X-Cache": "HIT" if all(cached) else "MISS",
                        "X-Cache-Key": full_key,
                    },
                    self._timed_chunks(self._stream_split(req, parts), t0, provider),
                )
        provider, model, of, params_canon, key = self._resolve(req)

        record = await self._metadata.get(key)
        if record and not self._expired(record):
            t_cs = time.perf_counter()
            native = await self._blobs.get(record.storage_path)
            if _same_format(
                of.encoding, of.sample_rate, record.encoding, record.sample_rate
            ):
                served, chunks = native, _chunked(native)
            else:
                served = await self._convert_audio(
                    native,
                    native_encoding=record.encoding,
                    native_rate=record.sample_rate,
                    out_encoding=of.encoding,
                    out_rate=of.sample_rate,
                )
                chunks = _chunked(served)
            await self._metrics.touch_and_record(
                key,
                {
                    "requests": 1,
                    "hits": 1,
                    "bytes_served": len(served),
                    "words_served": _wc(req.transcript),
                },
                provider=provider,
            )
            self._hits += 1
            logger.info(f"CACHE HIT  (stream) key={key[:12]}… provider={provider}")
            await self._timed("cache_serve", t_cs, provider)
            return {"X-Cache": "HIT", "X-Cache-Key": key}, self._timed_chunks(
                chunks, t0, provider
            )

        logger.info(
            f"CACHE MISS (stream) key={key[:12]}… provider={provider} — streaming synth"
        )

        instance = self._get_provider(provider)
        if instance is None:
            raise ProviderNotConfigured(provider)

        if _same_format(
            of.encoding,
            of.sample_rate,
            instance.native_encoding,
            instance.native_sample_rate,
        ):
            # Single-flight: if a synth is already in-flight for this key (bytes
            # or stream path), coalesce onto it — await the result and stream the
            # completed clip — instead of opening a 2nd provider stream. Else
            # become the producer: register the future BEFORE any await so a
            # concurrent request coalesces onto us (loop-thread-atomic).
            fut = self._inflight.get(key)
            if fut is not None and not fut.done():
                _h, _g = await self._stream_coalesced(
                    req, key, fut, of, provider, instance, model, params_canon, record
                )
                return _h, self._timed_chunks(_g, t0, provider)
            fut = self._new_inflight(key)
            return (
                {"X-Cache": "MISS", "X-Cache-Key": key},
                self._timed_chunks(
                    self._stream_and_store(
                        req,
                        instance,
                        key,
                        provider,
                        model,
                        params_canon,
                        record,
                        instance.native_encoding,
                        instance.native_sample_rate,
                        fut,
                    ),
                    t0,
                    provider,
                ),
            )

        # Requested format differs from native: synth fully, store native, convert.
        native = await self._synthesize(req, provider, model)
        if settings.enable_write_through:
            await self._store(
                key,
                req,
                provider,
                model,
                params_canon,
                native.audio,
                native.encoding,
                native.sample_rate,
                existing=record,
            )
        audio = await self._convert_audio(
            native.audio,
            native_encoding=native.encoding,
            native_rate=native.sample_rate,
            out_encoding=of.encoding,
            out_rate=of.sample_rate,
        )
        await self._metrics.record_metrics(
            provider=provider,
            requests=1,
            misses=1,
            bytes_served=len(audio),
            synth_calls=1,
            words_served=_wc(req.transcript),
            words_synthesized=_wc(req.transcript),
        )
        self._misses += 1
        return {"X-Cache": "MISS", "X-Cache-Key": key}, self._timed_chunks(
            _chunked(audio), t0, provider
        )

    async def _stream_and_store(
        self,
        req: TTSRequest,
        instance: BaseTTSProvider,
        key: str,
        provider: str,
        model: str,
        params_canon: str,
        record,
        native_encoding: str,
        native_sample_rate: int,
        fut: asyncio.Future,
    ) -> AsyncGenerator[bytes, None]:
        """Forward native provider chunks to the caller; store native on success.

        Partial audio is never cached: if the consumer stops early (client
        disconnect) or the provider errors, ``completed`` stays False and the
        accumulated bytes are discarded. The inner provider generator is closed
        explicitly so its socket is released promptly.
        """
        accumulated = bytearray()
        completed = False
        gate = get_gate(provider)
        gen = instance.stream_synth(
            text=prepend_leading_dot(req.transcript, provider),
            voice_id=req.voice.id,
            model=model,
            language=req.language,
            params=req.params,
        )
        try:
            # Hold the gate for the whole stream — it IS an in-flight synth.
            async with gate:
                async for chunk in gen:
                    accumulated += chunk
                    yield chunk
                completed = True
        finally:
            await gen.aclose()
            if completed and accumulated:
                audio = bytes(accumulated)
                if settings.enable_write_through:
                    await self._store(
                        key,
                        req,
                        provider,
                        model,
                        params_canon,
                        audio,
                        native_encoding,
                        native_sample_rate,
                        existing=record,
                    )
                await self._metrics.record_metrics(
                    provider=provider,
                    requests=1,
                    misses=1,
                    bytes_served=len(audio),
                    synth_calls=1,
                    words_served=_wc(req.transcript),
                    words_synthesized=_wc(req.transcript),
                )
                self._misses += 1
                if not fut.done():
                    fut.set_result((audio, native_encoding, native_sample_rate, "MISS"))
                logger.info(
                    f"CACHE STORE (stream) key={key[:12]}… provider={provider} size={len(audio)}B"
                )
            elif not fut.done():
                # Aborted (client disconnect) or provider error: partial audio is
                # discarded (never cached). Signal coalesced waiters to fall back
                # to their own synth rather than 502 on someone else's failure.
                fut.set_exception(ProviderError("stream synth failed or aborted"))
            if self._inflight.get(key) is fut:
                self._inflight.pop(key, None)

    async def _stream_coalesced(
        self,
        req,
        key,
        fut,
        of,
        provider,
        instance,
        model,
        params_canon,
        record,
    ) -> tuple[dict, AsyncGenerator[bytes, None]]:
        """Serve a streaming MISS by awaiting an in-flight producer for ``key``.

        The producer (bytes- or stream-path) is already synthesizing; we wait for
        its result, then stream the completed clip in chunks (a coalesced HIT). If
        the producer aborted/failed, fall through to our own live synth.

        On the failure path the producer's ``finally`` (which pops ``_inflight``)
        runs AFTER ``set_exception`` schedules this waiter, so ``_inflight`` may
        STILL hold the producer's now-done future when we reach the re-check
        below. The ``not existing.done()`` guard there is load-bearing — it skips
        that dead future (and any other done entry) so we coalesce only onto a
        genuinely-live producer and never loop back onto the one that just failed.
        Do NOT remove it on the assumption the dict is already clear.
        (CancelledError is NOT caught: a cancelled waiter dies.)
        """
        try:
            native, enc, rate, _status = await fut
        except Exception:
            # Producer failed. Another waiter may already have become the new
            # producer — re-check _inflight and coalesce onto it instead of each
            # waiter spawning its own synth (bounds a producer failure to ONE
            # retry, not N).
            existing = self._inflight.get(key)
            if existing is not None and not existing.done():
                return await self._stream_coalesced(
                    req,
                    key,
                    existing,
                    of,
                    provider,
                    instance,
                    model,
                    params_canon,
                    record,
                )
            fut = self._new_inflight(key)
            return (
                {"X-Cache": "MISS", "X-Cache-Key": key},
                self._stream_and_store(
                    req,
                    instance,
                    key,
                    provider,
                    model,
                    params_canon,
                    record,
                    instance.native_encoding,
                    instance.native_sample_rate,
                    fut,
                ),
            )
        served = (
            native
            if _same_format(of.encoding, of.sample_rate, enc, rate)
            else await self._convert_audio(
                native,
                native_encoding=enc,
                native_rate=rate,
                out_encoding=of.encoding,
                out_rate=of.sample_rate,
            )
        )
        await self._metrics.touch_and_record(
            key,
            {
                "requests": 1,
                "hits": 1,
                "bytes_served": len(served),
                "words_served": _wc(req.transcript),
            },
            provider=provider,
        )
        self._hits += 1
        logger.info(f"CACHE HIT (stream-coalesced) key={key[:12]}… provider={provider}")
        return {"X-Cache": "HIT", "X-Cache-Key": key}, _chunked(served)

    # -- admin ops -----------------------------------------------------------

    async def check(self, req: TTSRequest):
        """Return (cached, record_or_none, provider, model, key). No synthesis."""
        provider, model, of, params_canon, key = self._resolve(req)
        record = await self._metadata.get(key)
        cached = bool(record and not self._expired(record))
        return cached, record, provider, model, key

    async def create(self, req: TTSRequest, audio_override: bytes | None = None):
        """Force create/override. Returns
        (key, status, source, size_bytes, provider, model, stored_encoding,
        stored_sample_rate).

        Synthesized audio is stored in native format; a base64 override is
        stored as-is in the requested output_format (the caller asserts the
        supplied audio matches it). The returned encoding/rate is what's
        ACTUALLY stored. Either way the format-agnostic key means the entry
        serves every requested format on read.
        """
        provider, model, of, params_canon, key = self._resolve(req)
        existing = await self._metadata.get(key)
        overridden = bool(existing and not self._expired(existing))

        if audio_override is not None:
            audio = audio_override
            store_encoding, store_rate = of.encoding, of.sample_rate
            source = "base64"
        else:
            native = await self._synthesize(req, provider, model)
            audio = native.audio
            store_encoding, store_rate = native.encoding, native.sample_rate
            source = "synth"

        await self._store(
            key,
            req,
            provider,
            model,
            params_canon,
            audio,
            store_encoding,
            store_rate,
            existing=existing,
            replace=True,
        )
        if source == "synth":
            await self._metrics.record_metrics(creates=1, synth_calls=1)
        else:
            await self._metrics.record_metrics(creates=1, base64_uploads=1)

        status = "OVERRIDDEN" if overridden else "CREATED"
        logger.info(
            f"CREATE {status} source={source} key={key[:12]}… "
            f"provider={provider} size={len(audio)}B"
        )
        return (
            key,
            status,
            source,
            len(audio),
            provider,
            model,
            store_encoding,
            store_rate,
        )

    def _request_from_record(self, record: CacheRecord) -> TTSRequest:
        """Rebuild a TTSRequest from a stored CacheRecord.

        Used by resynth_by_key / replace_audio_by_key so the derived key
        round-trips (the same provider/model/text/language/params/output_format
        that originally produced ``record.key`` must re-derive to it).
        """
        try:
            params = json.loads(record.params) if record.params else {}
        except (ValueError, TypeError):
            params = {}
        return TTSRequest(
            model_id=f"{record.provider}:{record.model}",
            transcript=record.text or "",
            voice=CartesiaVoice(id=record.voice_id),
            language=record.language or "",
            output_format=OutputFormat(
                container=record.container,
                encoding=record.encoding,
                sample_rate=record.sample_rate,
            ),
            params=params,
        )

    async def resynth_by_key(self, key: str):
        """Re-synthesize an entry from its stored metadata, replacing the blob.

        Reads the record, rebuilds the request, asserts the key round-trips, and
        delegates to :meth:`create` (forces a fresh synth with ``replace=True``).
        Raises ``KeyError`` if the key is absent; ``ValueError`` if the stored
        fields no longer re-derive to the same key (the API maps that to 400).
        Returns the same tuple as :meth:`create`.
        """
        record = await self._metadata.get(key)
        if not record:
            raise KeyError(key)
        req = self._request_from_record(record)
        _p, _m, _of, _canon, derived = self._resolve(req)
        if derived != key:
            raise ValueError(
                f"key round-trip mismatch: stored {key!r} vs derived {derived!r}"
            )
        return await self.create(req)

    async def replace_audio_by_key(self, key: str, audio: bytes):
        """Replace an entry's audio blob with a user-supplied recording.

        Decodes the upload (WAV) to mono PCM s16le, converts to the entry's
        native format, and delegates to :meth:`create` with ``audio_override``
        (stores under the same key with ``replace=True``). Raises ``KeyError`` /
        ``ValueError`` like :meth:`resynth_by_key`; ``ValueError`` also covers an
        undecodable upload. Returns the same tuple as :meth:`create`.
        """
        record = await self._metadata.get(key)
        if not record:
            raise KeyError(key)
        native_pcm, native_rate = _decode_upload_to_pcm(audio)  # raises ValueError
        native = await self._convert_audio(
            native_pcm,
            native_encoding="pcm_s16le",
            native_rate=native_rate,
            out_encoding=record.encoding,
            out_rate=record.sample_rate,
        )
        req = self._request_from_record(record)
        _p, _m, _of, _canon, derived = self._resolve(req)
        if derived != key:
            raise ValueError(
                f"key round-trip mismatch: stored {key!r} vs derived {derived!r}"
            )
        return await self.create(req, audio_override=native)

    async def delete(self, req: TTSRequest):
        """Delete by derived key. Returns (deleted_bool, key).

        The store adjusts provider_totals atomically inside the DELETE
        transaction (using the row's actual size at delete time), so a concurrent
        override of the same key can't make totals drift."""
        provider, model, of, params_canon, key = self._resolve(req)
        record = await self._metadata.get(key)
        if not record:
            return False, key
        # storage_path is key-derived (content-addressed), so it's correct even
        # if a concurrent override rewrote the row's bytes between get and delete.
        if not await self._metadata.delete(key):
            return False, key
        await self._blobs.delete(record.storage_path)
        await self._metrics.record_metrics(deletes=1)
        logger.info(f"DELETE key={key[:12]}… provider={provider}")
        return True, key

    async def clear(
        self, provider: str | None = None, voice_id: str | None = None
    ) -> int:
        """Delete all entries (optionally filtered). Returns count removed.

        The store adjusts provider_totals atomically inside the DELETE
        transaction (SELECT+DELETE in one txn, so a concurrent insert can't
        escape the clear)."""
        deleted = await self._metadata.delete_filtered(
            provider=provider, voice_id=voice_id
        )
        for _prov, _size, path in deleted:
            await self._blobs.delete(path)
        if deleted:
            await self._metrics.record_metrics(deletes=len(deleted))
        logger.info(
            f"CLEAR removed {len(deleted)} entries (provider={provider}, voice_id={voice_id})"
        )
        return len(deleted)

    async def purge_expired(self) -> int:
        """Delete entries whose TTL has expired — metadata first (one atomic
        transaction, totals kept consistent), then unlink blobs.

        Order matters: if a blob unlink fails we log and continue — the row is
        already gone, so an orphaned file is harmless (reaped by
        :meth:`reconcile_blobs` at next startup) rather than leaving metadata
        pointing at a missing file. Returns the number of entries purged."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        rows = await self._metadata.purge_expired(now_iso)
        for _prov, _size, path in rows:
            try:
                await self._blobs.delete(path)
            except Exception as e:
                logger.warning(f"purge: blob delete failed for {path}: {e}")
        if rows:
            logger.info(f"TTL purge: removed {len(rows)} expired entries")
        return len(rows)

    async def backfill_missing_ttl(self) -> int:
        """One-shot admin op: give every pre-existing NULL-TTL row a RANDOM
        expiry in [CACHE_TTL_BACKFILL_MIN_HOURS, MAX_HOURS] so legacy entries
        (created before length-scaled TTL, e.g. under ttl_seconds=0) age out
        instead of living forever. Idempotent — only touches NULL rows. Trigger
        via POST /cache/backfill-ttl (not at startup). Returns rows backfilled."""
        n = await self._metadata.backfill_missing_ttl(
            settings.cache_ttl_backfill_min_hours,
            settings.cache_ttl_backfill_max_hours,
        )
        logger.info(f"TTL backfill: set random expiry on {n} NULL-TTL entries")
        return n

    # -- analytics + cache control -------------------------------------------

    @staticmethod
    def _derive(m: dict) -> dict:
        """Add hit_rate (+ words_from_cache_pct when the raw sums exist) to a
        metrics map. Per-provider rows carry words_synthesized, so
        words_from_cache_pct is derived for them too."""
        out = dict(m)
        req = m.get("requests", 0)
        out["hit_rate"] = round(m["hits"] / req, 4) if req else None
        wserved = m.get("words_served", 0)
        wsyn = m.get("words_synthesized")
        if wserved and wsyn is not None:
            # clamp at 0 — a negative savings rate is nonsensical.
            out["words_from_cache_pct"] = max(0, int((wserved - wsyn) * 100 / wserved))
        return out

    async def daily_stats(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """Extensive day-wise metrics: per date, derived totals (hit_rate,
        words_from_cache_pct on top of the raw sums) and a per-provider
        breakdown. ``provider`` narrows the view to one provider —
        the day's totals then reflect that provider only."""
        raw = await self._metadata.daily_summary(from_date, to_date)
        lat_by_day = await self._metadata.latency_summary_daily(from_date, to_date)
        days = []
        for date in sorted(raw):
            byp_all = raw[date]["by_provider"]
            if provider:
                byp = {provider: byp_all[provider]} if provider in byp_all else {}
                base = dict(byp.get(provider, {}))
            else:
                byp = dict(byp_all)
                base = dict(raw[date]["totals"])
            days.append(
                {
                    "date": date,
                    "totals": self._derive(base),
                    "by_provider": {p: self._derive(m) for p, m in byp.items()},
                    "latency": lat_by_day.get(date, {}),
                }
            )
        return {"range": {"from": from_date, "to": to_date}, "days": days}

    async def _delete_rows(
        self, rows: list[dict], *, dry_run: bool, label: str
    ) -> None:
        """Blob cleanup for a delete_where result (metadata already gone). Logs +
        continues on a per-blob failure so one bad unlink can't abort the batch."""
        if dry_run or not rows:
            return
        for r in rows:
            try:
                await self._blobs.delete(r["storage_path"])
            except Exception as e:
                logger.warning(
                    f"{label}: blob delete failed for {r['storage_path']}: {e}"
                )
        await self._metrics.record_metrics(deletes=len(rows))

    @staticmethod
    def _entries(rows: list[dict]) -> list[dict]:
        return [
            {
                "key": r["key"],
                "provider": r["provider"],
                "voice_id": r["voice_id"],
                "text": r["text"],
            }
            for r in rows
        ]

    async def delete_by_text(
        self,
        text: str | None = None,
        provider: str | None = None,
        voice_id: str | None = None,
        match: str = "exact",
        dry_run: bool = True,
    ) -> dict:
        """Delete cache entries by text (exact or substring), optionally narrowed
        by provider/voice. ``dry_run`` defaults True — preview matches + keys
        without deleting; set False to delete. Returns matched/deleted counts and
        the matched entries."""
        clauses: list[str] = []
        args: list = []
        if text is not None:
            if match.lower() == "substring":
                # ESCAPE '\\' so a user '%'/_'_' in text matches literally, not as
                # a wildcard (else text='_' would match every single-char row).
                clauses.append("text LIKE ? ESCAPE '\\'")
                args.append(f"%{escape_like(text)}%")
            else:
                clauses.append("text = ?")
                args.append(text)
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        if voice_id:
            clauses.append("voice_id = ?")
            args.append(voice_id)
        if not clauses:
            # An empty filter would match (and with dry_run=false, DELETE) the
            # ENTIRE cache — that's what /cache/clear is for. Require >= 1 filter.
            raise ValueError(
                "delete-by-text requires at least one of text/provider/voice_id"
            )
        where_sql = " AND ".join(clauses)
        rows = await self._metadata.delete_where(where_sql, args, dry_run=dry_run)
        await self._delete_rows(rows, dry_run=dry_run, label="delete-by-text")
        return {
            "matched": len(rows),
            "deleted": 0 if dry_run else len(rows),
            "dry_run": dry_run,
            "entries": self._entries(rows),
        }

    async def delete_by_age(self, older_than_days: int, dry_run: bool = True) -> dict:
        """Delete entries older than ``older_than_days`` (by created_at). Same
        dry_run contract as delete_by_text."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        rows = await self._metadata.delete_where(
            "created_at < ?", [cutoff], dry_run=dry_run
        )
        await self._delete_rows(rows, dry_run=dry_run, label="delete-by-age")
        return {
            "matched": len(rows),
            "deleted": 0 if dry_run else len(rows),
            "dry_run": dry_run,
            "older_than_days": older_than_days,
            "entries": self._entries(rows),
        }

    async def clear_preview(
        self, provider: str | None = None, voice_id: str | None = None
    ) -> dict:
        """Dry-run preview for /cache/clear: count + bytes + per-provider breakdown
        of what WOULD be deleted, without touching anything."""
        clauses: list[str] = []
        args: list = []
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        if voice_id:
            clauses.append("voice_id = ?")
            args.append(voice_id)
        where_sql = " AND ".join(clauses) if clauses else ""
        rows = await self._metadata.delete_where(where_sql, args, dry_run=True)
        by_prov: dict[str, list[int]] = {}
        for r in rows:
            d = by_prov.setdefault(r["provider"], [0, 0])
            d[0] += 1
            d[1] += r["size_bytes"]
        return {
            "would_delete": len(rows),
            "bytes": sum(r["size_bytes"] for r in rows),
            "by_provider": {
                p: {"entries": c, "bytes": b} for p, (c, b) in by_prov.items()
            },
        }

    async def reconcile_blobs(self) -> int:
        """Delete blob files that have no metadata row.

        Such orphans arise when a crash/failure lands between a row-delete
        commit and its blob unlink: delete/clear commit the row first to
        preserve the "never a row pointing at a missing blob" invariant, at the
        cost of a possible orphan FILE. Safe + idempotent to run at startup."""
        live = await self._metadata.all_keys()
        removed = 0
        async for key, rel_path in self._blobs.iter_blobs():
            if key not in live:
                if await self._blobs.delete(rel_path):
                    removed += 1
        if removed:
            logger.info(f"RECONCILE removed {removed} orphaned blob(s)")
        return removed

    @property
    def session_stats(self) -> dict:
        """Ephemeral hit/miss counters since process start."""
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "requests": total,
            "hit_rate": round(self._hits / total, 4) if total else None,
        }
