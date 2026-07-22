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
import random
import re
import time

import numpy as np
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.audio.format import convert_audio
from app.audio.text import normalize_for_tts, prepend_leading_dot
from app.cache.metrics import WriteBehindMetrics
from app.cache.resilience import get_gate
from app.cache.key import (
    canonical_params,
    hash_key,
    normalize_text,
    parse_model_id,
)
from app.cache.segment import MAX_SPAN, segment_dp
from app.core.config import settings
from app.core.logging import logger
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError
from app.providers.registry import ProviderNotConfigured
from app.schemas.tts import TTSRequest
from app.storage.base import CacheRecord, escape_like


def _wc(text: str | None) -> int:
    """Word count of a transcript (whitespace tokens, punctuation kept)."""
    return len((text or "").split())


# Sentinel: caller has NOT pre-fetched the existing record (vs. None = checked,
# genuinely absent). Avoids a redundant get() on every store when the caller
# already knows.
_UNCHECKED = object()

# Chunk size used when streaming a complete blob back to the caller (HIT, or a
# one-shot miss). Large enough to amortize per-iteration overhead, small enough
# to start flowing promptly.
_STREAM_CHUNK = 16 * 1024


async def _chunked(data: bytes, size: int = _STREAM_CHUNK) -> AsyncGenerator[bytes, None]:
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

    ``min_words`` gates the split: it only fires when EVERY part has at least that
    many words. A lone single-word part (e.g. "hello" in "hello. how are you") is
    too small to cache on its own and would needlessly fragment the phrase, so when
    any part is shorter the WHOLE text is returned as one chunk.

    Returns ``[text]`` (one chunk) when ``symbols`` is empty, no split point
    exists, or the min-words gate fails — so a caller's ``len(parts) > 1`` check
    is the on/off gate.

    Note: abbreviations like "Mr. Smith" WILL split (no dictionary) — acceptable
    for the scripted-greeting use case; set ``symbols`` to "." only to minimize it.
    """
    if not symbols or not text or not text.strip():
        return [text] if (text and text.strip()) else []
    parts = [p.strip() for p in re.split(rf"(?<=[{re.escape(symbols)}])\s+", text) if p.strip()]
    # Min-words gate: if any part is too short, keep the whole phrase as one entry.
    if len(parts) > 1 and any(len(p.split()) < min_words for p in parts):
        return [text]
    return parts


# Split parts are pure-concatenated — no edge trim, no inserted gap — so nothing
# is cut and each part's audio is appended exactly as the provider returned it
# (see _get_or_synthesize_split / _stream_split). The silence-trim DSP below is
# for stitch assembly only.


# --- stitch assembly DSP (numpy) ------------------------------------------
# Assembles native pcm_s16le@16k mono sub-phrase clips into one continuous clip.
# Choppiness in stitched TTS comes from three cheap-to-fix things — not pitch:
#   1. clicks/pops  -> DC-offset removal + splice at a zero crossing
#   2. doubled silence -> edge silence-trim (with a guard so onsets survive)
#   3. loudness mismatch -> per-clip RMS normalization to a common target
# plus an equal-power (constant-power) crossfade so the seam doesn't dip.
# Pure numpy: no heavy voice deps, and this is also the audioop->Py3.13 path.

_SR = 16_000  # native sample rate (pcm_s16le@16k) — fixed (audio format), not tunable

# Seam-DSP knobs — env-tunable (PREDICTIVE_STITCH_XFADE_MS, _TARGET_RMS_DB,
# _RMS_FLOOR_DB, _SIL_RELATIVE_DB, _SIL_GUARD_MS, _ZC_SEARCH_MS). Read once at
# import; change via env + restart. See app/core/config.py for what each does.
_XFADE_MS = settings.predictive_stitch_xfade_ms
_TARGET_RMS_DB = settings.predictive_stitch_target_rms_db
_RMS_FLOOR_DB = settings.predictive_stitch_rms_floor_db
_SIL_RELATIVE_DB = settings.predictive_stitch_sil_relative_db
_SIL_GUARD_MS = settings.predictive_stitch_sil_guard_ms
_ZC_SEARCH_MS = settings.predictive_stitch_zc_search_ms


def _to_float(b: bytes) -> np.ndarray:
    """int16 LE pcm bytes -> float32 in [-1, 1]."""
    return np.frombuffer(b, dtype="<i2").astype(np.float32) / 32768.0


def _to_int16(x: np.ndarray) -> bytes:
    """float32 [-1, 1] -> int16 LE pcm bytes."""
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _prep_clip(p: bytes) -> np.ndarray:
    """Per-clip stitch prep: float -> silence-trim -> DC-remove -> RMS-normalize.

    Each clip is normalized to the SAME fixed target (-20 dBFS) independently, so
    a clip prepped and emitted early (progressive path) is loudness-consistent
    with one prepped later in the same run. Extracted from _stitch_clips so the
    assemble path and the streaming path prepare clips identically.
    """
    x = _to_float(p)
    x = _trim_edges(x, head=True, tail=True)  # trim silence on the raw signal first
    x = x - float(np.mean(x))                  # DC removal over the voiced part
    x = _rms_normalize(x)
    return x


def _rms_normalize(x: np.ndarray) -> np.ndarray:
    """Scale so the clip's RMS hits the target loudness; leave near-silent clips."""
    rms = float(np.sqrt(np.mean(x * x))) + 1e-12
    db = 20.0 * np.log10(rms)
    if db < _RMS_FLOOR_DB:
        return x
    return x * (10.0 ** ((_TARGET_RMS_DB - db) / 20.0))


def _trim_edges(x: np.ndarray, head: bool, tail: bool) -> np.ndarray:
    """Trim leading/trailing silence (windowed RMS), keeping a small guard.

    The gate is ADAPTIVE — relative to this clip's loudest window, floored near
    zero — so it works across voices/volumes and catches the low-amplitude
    trailing decay Cartesia leaves on every clip. A fixed absolute threshold let
    that decay survive, and those tails stacked into the audible pauses between
    stitched fragments."""
    n = len(x)
    if n == 0:
        return x
    win = max(1, int(_SR * 1.0 / 1000))  # 1ms RMS window
    energy = np.convolve(x * x, np.ones(win, dtype=np.float32) / win, mode="same")
    peak = float(energy.max()) + 1e-12
    thresh = max(peak * (10.0 ** (-_SIL_RELATIVE_DB / 10.0)), 1e-9)
    loud = energy > thresh
    if not loud.any():
        return x  # all-silent (e.g. test fixtures): leave untouched
    first = int(np.argmax(loud))
    last = n - int(np.argmax(loud[::-1]))
    guard = int(_SR * _SIL_GUARD_MS / 1000)
    start = max(0, first - guard) if head else 0
    end = min(n, last + guard) if tail else n
    if start >= end:
        return x
    return x[start:end]


def _snap_zero(x: np.ndarray, side: str) -> int:
    """Index near the head/tail edge where the signal crosses zero (minimizes the
    step discontinuity at the splice). Falls back to the raw edge if none nearby."""
    span = min(int(_SR * _ZC_SEARCH_MS / 1000), len(x))
    if span < 2:
        return 0 if side == "head" else len(x)
    if side == "head":
        seg = x[:span]
        zc = np.where(np.diff(np.signbit(seg)))[0]
        return int(zc[0]) if zc.size else 0
    seg = x[-span:]
    zc = np.where(np.diff(np.signbit(seg)))[0]
    return (len(x) - span + int(zc[-1] + 1)) if zc.size else len(x)


def _equal_power_xfade(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Overlap a's tail with b's head via a constant-power (cos/sin) crossfade.
    cos^2+sin^2=1 keeps total power constant through the fade, avoiding the -3dB
    midpoint dip a linear fade produces on uncorrelated speech."""
    n = min(int(_SR * _XFADE_MS / 1000), len(a), len(b))
    if n <= 0:
        return np.concatenate([a, b])
    t = np.linspace(0.0, np.pi / 2.0, n, dtype=np.float32)  # 0 -> pi/2
    blended = a[-n:] * np.cos(t) + b[:n] * np.sin(t)
    return np.concatenate([a[:-n], blended, b[n:]])


def _stitch_clips(pieces: list[bytes]) -> bytes:
    """Assemble native pcm_s16le@16k clips into one continuous clip.

    Per clip: DC-remove -> silence-trim -> RMS-normalize; then splice each pair
    at zero crossings with an equal-power crossfade. Returns native int16 bytes.
    """
    if not pieces:
        return b""
    clips: list[np.ndarray] = [_prep_clip(p) for p in pieces]
    if len(clips) == 1:
        return _to_int16(clips[0])
    out = clips[0]
    for nxt in clips[1:]:
        out = out[: _snap_zero(out, "tail")]
        nxt = nxt[_snap_zero(nxt, "head"):]
        out = _equal_power_xfade(out, nxt)
    return _to_int16(out)


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
        # Optional predictive-warmer observer (set via attach_tracker). Never
        # blocks or breaks reads; observation is fast and warming is async.
        self._tracker = None

    def attach_tracker(self, tracker) -> None:
        self._tracker = tracker

    async def start(self) -> None:
        """Start background tasks (write-behind metrics flusher)."""
        if hasattr(self._metrics, "start"):
            self._metrics.start()

    async def stop(self) -> None:
        """Flush + stop background tasks (graceful shutdown loses no metrics)."""
        if hasattr(self._metrics, "stop"):
            await self._metrics.stop()

    async def _observe(self, *, text, provider, voice_id, model, language, params) -> None:
        if self._tracker is None:
            return
        try:
            await self._tracker.observe(
                text=text,
                provider=provider,
                voice_id=voice_id,
                model=model,
                language=language,
                params=params,
            )
        except Exception as e:
            logger.warning(f"cache observe failed: {e}")

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
        # _synthesize / _stream_and_store / warm_split). A dot-free key means
        # stitch sub-span keys match cached entries naturally.
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
            and datetime.fromisoformat(record.ttl_expires_at) <= datetime.now(timezone.utc)
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

    async def _synthesize(self, req: TTSRequest, provider: str, model: str) -> AudioResult:
        """Synthesize via the routed provider; return its NATIVE-format audio.

        Runs under the provider's resilience gate (bulkhead + rate limit) so a
        slow/hung provider is capped and can't starve the others.
        """
        instance = self._get_provider(provider)
        if instance is None:
            raise ProviderNotConfigured(provider)
        gate = get_gate(provider)
        async with gate:
            t0 = time.perf_counter()
            result = await instance.synth(
                text=prepend_leading_dot(req.transcript, provider),
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
        every stored entry (write-through AND warmer) — there's no permanent
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
        self, data: bytes, *, native_encoding: str, native_rate: int,
        out_encoding: str, out_rate: int,
    ) -> bytes:
        """convert_audio off the event loop.

        audioop resample/μ-law conversion is CPU-bound and runs on every serve
        (HIT and MISS); inline it would block the loop (and starve the Cartesia
        socket pumps) under concurrent load, so dispatch it to the worker pool.
        """
        return await asyncio.to_thread(
            convert_audio, data,
            native_encoding=native_encoding, native_rate=native_rate,
            out_encoding=out_encoding, out_rate=out_rate,
        )

    # -- read path -----------------------------------------------------------

    async def _stitch_plan(
        self, req: TTSRequest, provider: str, model: str, params_canon: str,
    ):
        """Shared span planning for :meth:`stitch` and the pass-through stream path.

        Returns ``(spans, span_record, words, n)`` or ``None`` if stitching isn't
        worthwhile (disabled, <2 words, or cached coverage below the gate).
        ``spans`` tiles ``[0,n)`` as ``(lo, hi, is_cached)``; ``span_record`` maps
        a cached ``(lo,hi)`` to its non-expired record. Pure extraction of
        ``stitch``'s head -- identical behavior (existing tests are the safety net).
        """
        if not settings.predictive_stitch_enabled:
            return None
        words = normalize_text(req.transcript).split()
        n = len(words)
        if n < 2:
            return None
        # One batched lookup: every candidate (lo, hi) key (hi-lo <= MAX_SPAN).
        # Sub-span keys use the SAME number-only normalization as the stored
        # entries (the dot is a synth-only hint, NOT in the key), so any cached
        # sub-span matches directly. Gap spans synth via _synthesize, which
        # applies the ElevenLabs dot, so cached + gap clips share onset/prosody.
        key_to_span: dict[str, tuple[int, int]] = {}
        for lo in range(n):
            for hi in range(lo + 1, min(lo + MAX_SPAN, n) + 1):
                sub_text = normalize_for_tts(" ".join(words[lo:hi]), provider)
                key_to_span[hash_key(
                    text=sub_text, provider=provider, voice_id=req.voice.id,
                    model=model, language=req.language, params_canonical=params_canon,
                )] = (lo, hi)
        records = await self._metadata.get_many(key_to_span.keys())
        span_record = {  # (lo, hi) -> record, only non-expired
            key_to_span[k]: r for k, r in records.items() if not self._expired(r)
        }
        spans = segment_dp(n, set(span_record))
        cached_words = sum(hi - lo for lo, hi, c in spans if c)
        if cached_words == 0 or cached_words / n < settings.predictive_stitch_min_coverage:
            return None  # not enough cached -> caller should synth the whole phrase
        return spans, span_record, words, n

    async def _span_bytes(
        self, lo: int, hi: int, c: bool, span_record, req: TTSRequest,
        provider: str, model: str, words: list[str],
    ) -> bytes:
        """Raw native pcm bytes for one span: cached blob (with eviction
        fallthrough) or synthesized gap. Shared by :meth:`stitch` and the
        progressive stream path."""
        if c:
            rec = span_record.get((lo, hi))
            if rec:  # non-expired (filtered in _stitch_plan); reuse its clip
                try:
                    return await self._blobs.get(rec.storage_path)
                except FileNotFoundError:
                    pass  # evicted between probe and fetch -> synth this span
        return await self._synth_span(req, provider, model, words, lo, hi)

    async def _synth_span(
        self, req: TTSRequest, provider: str, model: str, words: list[str],
        lo: int, hi: int,
    ) -> bytes:
        """Synthesize one gap span (``words[lo:hi]``) to native pcm and return it."""
        sub_req = TTSRequest(
            model_id=req.model_id, transcript=" ".join(words[lo:hi]), voice=req.voice,
            language=req.language, output_format=req.output_format, params=req.params,
        )
        native = await self._synthesize(sub_req, provider, model)
        return native.audio

    async def stitch(
        self, req: TTSRequest, provider: str, model: str, params_canon: str, plan=None,
    ):
        """Serve a full-text MISS from cached phrases where possible.

        Probes every candidate sub-span of the phrase in ONE batched key lookup,
        then DP-segments into cached/synth spans maximizing cached coverage and
        synthesizes only the gaps. No substring-closure assumption, so it works
        with a whole-phrase cache (the warmer need not pre-split into a
        sub-phrase lattice). Returns native ``pcm_s16le@16k`` audio, or ``None``
        if stitching isn't worthwhile (disabled, too short, or cached coverage
        below the gate) so the caller synthesizes the whole phrase.

        ``plan`` optionally carries a precomputed ``_stitch_plan`` result so a
        caller that already ran the plan (e.g. the pass-through eligibility check)
        can fall back here without re-running the batched lookup + DP a 2nd time.
        """
        if plan is None:
            plan = await self._stitch_plan(req, provider, model, params_canon)
        if plan is None:
            return None
        spans, span_record, words, n = plan

        pieces = await asyncio.gather(*(
            self._span_bytes(lo, hi, c, span_record, req, provider, model, words)
            for (lo, hi, c) in spans
        ))
        cached_words = sum(hi - lo for lo, hi, c in spans if c)
        gap_words = n - cached_words
        await self._metrics.record_metrics(
            provider=provider,
            words_synthesized=gap_words,
            stitch_calls=1,
            stitch_words_assembled=cached_words,
            stitch_words_synthesized=gap_words,
        )
        logger.info(
            f"STITCH key=… provider={provider} words={n} "
            f"spans={len(spans)} coverage={cached_words}/{n}"
        )
        return await asyncio.to_thread(_stitch_clips, pieces)

    async def _progressive_stitch_stream(
        self,
        req: TTSRequest,
        provider: str,
        model: str,
        params_canon: str,
        key: str,
        record,
        spans: list[tuple[int, int, bool]],
        span_record: dict,
        words: list[str],
        n: int,
    ) -> AsyncGenerator[bytes, None]:
        """Stream a stitch MISS progressively: emit the cached PREFIX immediately
        (TTFB ~0) while the gaps synthesize, holding only the ~xfade window per seam.

        Cached spans are fetched (fast local-FS read) and emitted up front; every
        GAP synth is kicked off concurrently up front (like assemble's gather), so
        total ~= max(gap) -- not the sum of gaps -- while the prefix still streams
        first. Seams reuse ``_prep_clip`` + ``_snap_zero`` + ``_equal_power_xfade``
        (fixed-target RMS via ``_prep_clip`` keeps loudness consistent across
        early-emitted and later clips). On clean completion the assembled clip is
        stored (if write-through) so repeats HIT; pending gap synths are cancelled
        on disconnect / early break so no provider call outlives the stream.
        ``key`` is passed in -- never re-resolved (re-normalizing an already-
        normalized transcript would re-hash).
        """
        xfade_n = int(_SR * _XFADE_MS / 1000)
        accumulated = bytearray()          # assembled native pcm, for store-on-complete
        out: np.ndarray | None = None      # held tail from the previous clip
        last_idx = len(spans) - 1
        completed = False
        synth_done = 0                      # gap synths that actually completed
        synth_words_done = 0               # words in those completed gaps

        async def emit(buf: np.ndarray) -> AsyncGenerator[bytes, None]:
            payload = _to_int16(buf)
            accumulated.extend(payload)
            async for chunk in _chunked(payload):
                yield chunk

        # Kick off every GAP synth concurrently up front. Cached spans are fetched
        # inline (fast local-FS reads); only gaps need prefetching. Gaps run in
        # parallel (like assemble's gather) -> total ~= max(gap), while the cached
        # prefix still streams first (TTFB ~0).
        gap_tasks: dict[int, asyncio.Task] = {
            idx: asyncio.create_task(
                self._span_bytes(lo, hi, False, span_record, req, provider, model, words)
            )
            for idx, (lo, hi, c) in enumerate(spans) if not c
        }

        try:
            for idx, (lo, hi, c) in enumerate(spans):
                # Cached span = fast local-FS read; a gap = await its already-running
                # concurrent task (after earlier prefix bytes were yielded). A failure
                # mid-stream (headers/prefix committed) can't become a clean 500, so
                # stop gracefully and credit only synth work that actually finished.
                try:
                    if c:
                        raw = await self._span_bytes(lo, hi, c, span_record, req, provider, model, words)
                    else:
                        raw = await gap_tasks[idx]
                except Exception as e:
                    logger.warning(
                        f"pass-through stitch span ({lo}:{hi} cached={c}) failed: "
                        f"{e}; truncating stream at prefix"
                    )
                    # Flush the held tail from the last good span so the caller
                    # keeps the audio it already earned (no abrupt last-bit
                    # cutoff). Done here, NOT in finally: yielding under
                    # GeneratorExit (client disconnect mid-stream) would raise,
                    # and a disconnecting caller won't consume the extra chunk
                    # anyway. `accumulated` grows too, but `completed` stays
                    # False so nothing is stored (partial-clip invariant holds).
                    if out is not None and len(out):
                        async for chunk in emit(out):
                            yield chunk
                        out = None
                    break
                if not c:  # a gap span that synthesized successfully
                    synth_done += 1
                    synth_words_done += hi - lo

                clip = _prep_clip(raw)

                if out is None:
                    # First span: emit everything except the held xfade tail.
                    if idx == last_idx:
                        async for chunk in emit(clip):
                            yield chunk
                        out = None
                    else:
                        hold = min(xfade_n, len(clip))
                        emit_len = max(0, len(clip) - hold)
                        if emit_len:
                            async for chunk in emit(clip[:emit_len]):
                                yield chunk
                        out = clip[emit_len:]   # held tail (empty when hold == 0)
                    continue

                # Subsequent span: splice the held tail into this clip via xfade.
                out = out[: _snap_zero(out, "tail")]
                nxt = clip[_snap_zero(clip, "head"):]
                joined = _equal_power_xfade(out, nxt)

                if idx == last_idx:
                    async for chunk in emit(joined):
                        yield chunk
                    out = None
                else:
                    hold = min(xfade_n, len(joined))
                    emit_len = max(0, len(joined) - hold)
                    if emit_len:
                        async for chunk in emit(joined[:emit_len]):
                            yield chunk
                    out = joined[emit_len:]
            else:
                completed = True  # loop ran to completion (no break / no exception)
        finally:
            # Cancel any gap synth still running (disconnect / early break) so no
            # provider call outlives the stream, and retrieve exceptions so asyncio
            # doesn't warn about unretrieved task exceptions.
            for _t in gap_tasks.values():
                if not _t.done():
                    _t.cancel()
                elif not _t.cancelled():
                    _t.exception()

            # Count the request/miss always (it happened); cache ONLY a cleanly-
            # completed assembled clip (no half clips on disconnect/error -- matches
            # _stream_and_store's partial-consumption invariant); and credit synth
            # work only for gaps that actually finished (so a disconnect/failed gap
            # doesn't inflate synth_calls/words_synthesized).
            audio = bytes(accumulated)
            cached_words = sum(hi - lo for lo, hi, c in spans if c)
            if completed and settings.enable_write_through and audio:
                try:
                    await self._store(
                        key, req, provider, model, params_canon,
                        audio, "pcm_s16le", 16000, existing=record,
                    )
                except Exception as e:
                    logger.warning(f"pass-through stitch store failed: {e}")
            await self._metrics.record_metrics(
                provider=provider,
                requests=1, misses=1, bytes_served=len(audio),
                synth_calls=synth_done,
                words_served=_wc(req.transcript), words_synthesized=synth_words_done,
                stitch_calls=1, stitch_words_assembled=cached_words,
                stitch_words_synthesized=synth_words_done,
            )
            self._misses += 1
            logger.info(
                f"CACHE MISS-STITCH-PASSTHROUGH (stream) key={key[:12]}… "
                f"provider={provider} words={n} coverage={cached_words}/{n} "
                f"synth={synth_done} "
                f"stored={'yes' if (completed and settings.enable_write_through and audio) else 'no'}"
            )

    async def _produce(
        self, req: TTSRequest, provider: str, model: str, params_canon: str,
        key: str, record,
    ) -> tuple[bytes, str, int, str]:
        """Synth + store for a MISS. Returns (native, encoding, rate, status)
        where status is 'MISS', 'MISS-STITCH', or 'HIT' (if the cache was filled
        between the outer lookup and here — e.g. by the warmer)."""
        rec = await self._metadata.get(key)
        if rec and not self._expired(rec):
            return await self._blobs.get(rec.storage_path), rec.encoding, rec.sample_rate, "HIT"
        stitched = await self.stitch(req, provider, model, params_canon)
        if stitched is not None:
            if settings.enable_write_through:
                await self._store(
                    key, req, provider, model, params_canon,
                    stitched, "pcm_s16le", 16000, existing=record,
                )
            return stitched, "pcm_s16le", 16000, "MISS-STITCH"
        native = await self._synthesize(req, provider, model)
        if settings.enable_write_through:
            await self._store(
                key, req, provider, model, params_canon,
                native.audio, native.encoding, native.sample_rate, existing=record,
            )
        return native.audio, native.encoding, native.sample_rate, "MISS"

    async def _produce_native(
        self, req: TTSRequest, provider: str, model: str, params_canon: str,
        key: str, record,
    ) -> tuple[bytes, str, int, str, bool]:
        """Single-flight: produce + store native audio for ``key`` exactly once.

        Concurrent MISSes for the same key share one synth + one store (the
        producer's future). Returns (native, encoding, rate, status, produced):
        ``produced`` is True for the caller that ran the synth (records a miss +
        synth_call) and False for a caller that coalesced (records a hit — served
        without its own Cartesia call).
        """
        fut = self._inflight.get(key)
        if fut is not None:
            native, enc, rate, status = await fut
            return native, enc, rate, status, False  # coalesced — no own synth
        fut = self._new_inflight(key)
        try:
            result = await self._produce(req, provider, model, params_canon, key, record)
            if not fut.done():
                fut.set_result(result)
            native, enc, rate, status = result
            # `produced` = a synth actually ran. _produce can short-circuit to
            # status='HIT' (the warmer filled the key between the outer MISS
            # lookup and the inner re-fetch) — that served a cache read, not a
            # synth, so it must count as a HIT, not a miss/synth_call.
            produced = status in ("MISS", "MISS-STITCH")
            return native, enc, rate, status, produced
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
                req.transcript, settings.split_at_symbols, settings.split_min_words_per_part
            )
            if len(parts) > 1:
                return await self._get_or_synthesize_split(req, parts)
        provider, model, of, params_canon, key = self._resolve(req)
        t0 = time.perf_counter()
        await self._observe(
            text=req.transcript, provider=provider, voice_id=req.voice.id,
            model=model, language=req.language, params=req.params,
        )

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
                key, {"requests": 1, "hits": 1, "bytes_served": len(audio),
                      "words_served": _wc(req.transcript)},
                provider=provider,
            )
            self._hits += 1
            logger.info(f"CACHE HIT  key={key[:12]}… provider={provider}")
            await self._timed("cache_serve", t_cs, provider)
            await self._timed("total", t0, provider)
            return audio, {"X-Cache": "HIT", "X-Cache-Key": key}

        logger.info(f"CACHE MISS key={key[:12]}… provider={provider} — synthesizing")

        # Single-flight: N concurrent identical misses share one synth + one store.
        native, nenc, nrate, status, produced = await self._produce_native(
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
                requests=1, misses=1, bytes_served=len(audio), synth_calls=1,
                words_served=_wc(req.transcript),
                # stitch() already recorded the gap as words_synthesized; for a
                # plain MISS the whole phrase was synthesized.
                words_synthesized=_wc(req.transcript) if status == "MISS" else 0,
            )
            self._misses += 1
            logger.info(f"CACHE {status} key={key[:12]}… provider={provider}")
        else:
            # Served from cache without our own synth: coalesced or warmer-filled.
            await self._metrics.touch_and_record(
                key, {"requests": 1, "hits": 1, "bytes_served": len(audio),
                      "words_served": _wc(req.transcript)},
                provider=provider,
            )
            self._hits += 1
            status = "HIT"
            logger.info(f"CACHE HIT (coalesced) key={key[:12]}… provider={provider}")
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
        self, req: TTSRequest, parts: list[str],
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
        chunks: list[bytes] = []
        all_hit = True
        for part in parts:
            part_req = req.model_copy(update={"transcript": part})
            audio, headers = await self.get_or_synthesize(part_req)
            chunks.append(audio)
            if headers.get("X-Cache") != "HIT":
                all_hit = False
        # Pure concatenation: append each part's audio verbatim — no trim, no gap
        # (nothing is cut; the result is synth(part0) + synth(part1) + ...).
        joined = b"".join(c for c in chunks if c)
        return joined, {
            "X-Cache": "HIT" if all_hit else "MISS",
            "X-Cache-Key": full_key,
        }

    async def _stream_split(
        self, req: TTSRequest, parts: list[str],
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
        t0 = time.perf_counter()  # entry; total = entry -> last byte (via _timed_chunks)
        # SPLIT_AT_SYMBOLS_STREAM: when set and the transcript splits into >1
        # sentence, stream each sentence independently (each caches under its own
        # key). Independent of SPLIT_AT_SYMBOLS (the /tts/bytes knob). Empty
        # (default) / no split -> skipped; the single-phrase stream path below
        # runs byte-for-byte unchanged. Each part is also observed inside
        # get_or_synthesize, so warming tracks sub-phrases (not the whole phrase).
        if settings.split_at_symbols_stream:
            parts = _split_transcript(
                req.transcript, settings.split_at_symbols_stream, settings.split_min_words_per_part
            )
            if len(parts) > 1:
                provider, _model, _of, _pc, full_key = self._resolve(req)
                # Headers must be set before streaming: pre-check each part's cache
                # so the aggregate X-Cache reflects whether any sentence will synth.
                cached = [
                    await self._is_cached(req.model_copy(update={"transcript": p}))
                    for p in parts
                ]
                return (
                    {"X-Cache": "HIT" if all(cached) else "MISS", "X-Cache-Key": full_key},
                    self._timed_chunks(self._stream_split(req, parts), t0, provider),
                )
        provider, model, of, params_canon, key = self._resolve(req)
        await self._observe(
            text=req.transcript, provider=provider, voice_id=req.voice.id,
            model=model, language=req.language, params=req.params,
        )

        record = await self._metadata.get(key)
        if record and not self._expired(record):
            t_cs = time.perf_counter()
            native = await self._blobs.get(record.storage_path)
            if _same_format(of.encoding, of.sample_rate, record.encoding, record.sample_rate):
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
                key, {"requests": 1, "hits": 1, "bytes_served": len(served),
                      "words_served": _wc(req.transcript)},
                provider=provider,
            )
            self._hits += 1
            logger.info(f"CACHE HIT  (stream) key={key[:12]}… provider={provider}")
            await self._timed("cache_serve", t_cs, provider)
            return {"X-Cache": "HIT", "X-Cache-Key": key}, self._timed_chunks(chunks, t0, provider)

        logger.info(
            f"CACHE MISS (stream) key={key[:12]}… provider={provider} — streaming synth"
        )

        # Predictive stitch: assemble from cached sub-phrases + synthesized gaps,
        # store the assembled clip, and stream it chunked (so future requests HIT).
        # Falls through to live streaming if not worthwhile. Unlike live streaming
        # this waits for the gaps to synth before first byte — but the assembled
        # clip is then cached, so repeats are instant HITs.
        if settings.predictive_stitch_stream_enabled:
            plan = None  # computed by the pass-through check; reused by the assemble fallback
            # --- Progressive pass-through (opt-in): stream the cached prefix
            # immediately while the first gap synthesizes (TTFB ~0). Falls back
            # to the assemble path below on any ineligibility/error, so this is
            # purely additive -- when ENABLE_PASS_THROUGH_STITCH is false the
            # block below runs unchanged. ---
            if settings.enable_pass_through_stitch and _same_format(
                of.encoding, of.sample_rate, "pcm_s16le", 16000
            ):
                try:
                    plan = await self._stitch_plan(req, provider, model, params_canon)
                except Exception as e:  # plan failure -> safe assemble fallback
                    logger.warning(f"pass-through stitch plan failed ({e}); assemble fallback")
                    plan = None
                if plan is not None:
                    spans, span_record, words, n = plan
                    first_gap = next((i for i, s in enumerate(spans) if not s[2]), None)
                    prefix_words = (
                        sum(hi - lo for lo, hi, c in spans[:first_gap] if c)
                        if first_gap is not None else 0
                    )
                    if first_gap is not None and prefix_words >= settings.pass_through_stitch_min_words:
                        return (
                            {"X-Cache": "MISS-STITCH", "X-Cache-Key": key},
                            self._timed_chunks(
                                self._progressive_stitch_stream(
                                    req, provider, model, params_canon, key, record,
                                    spans, span_record, words, n,
                                ),
                                t0,
                                provider,
                            ),
                        )
                    # prefix too short / no cached prefix before the first gap ->
                    # the assemble path below is just as good (no TTFB win to claim).

            # --- Existing assemble-then-stream stitch path (unchanged) ---
            # `plan` is reused when the pass-through check above ran but fell
            # through (ineligible), so the batched lookup + DP run only once.
            stitched = await self.stitch(req, provider, model, params_canon, plan=plan)
            if stitched is not None:
                if settings.enable_write_through:
                    await self._store(
                        key, req, provider, model, params_canon,
                        stitched, "pcm_s16le", 16000, existing=record,
                    )
                audio = await self._convert_audio(
                    stitched,
                    native_encoding="pcm_s16le",
                    native_rate=16000,
                    out_encoding=of.encoding,
                    out_rate=of.sample_rate,
                )
                await self._metrics.record_metrics(
                    provider=provider,
                    requests=1, misses=1, bytes_served=len(audio), synth_calls=1,
                    words_served=_wc(req.transcript), words_synthesized=0,
                )
                self._misses += 1
                logger.info(
                    f"CACHE MISS-STITCH (stream) key={key[:12]}… "
                    f"provider={provider} size={len(audio)}B"
                )
                return {"X-Cache": "MISS-STITCH", "X-Cache-Key": key}, self._timed_chunks(_chunked(audio), t0, provider)

        instance = self._get_provider(provider)
        if instance is None:
            raise ProviderNotConfigured(provider)

        if _same_format(of.encoding, of.sample_rate, instance.native_encoding, instance.native_sample_rate):
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
                        req, instance, key, provider, model, params_canon, record,
                        instance.native_encoding, instance.native_sample_rate, fut,
                    ),
                    t0,
                    provider,
                ),
            )

        # Requested format differs from native: synth fully, store native, convert.
        native = await self._synthesize(req, provider, model)
        if settings.enable_write_through:
            await self._store(
                key, req, provider, model, params_canon,
                native.audio, native.encoding, native.sample_rate, existing=record,
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
            requests=1, misses=1, bytes_served=len(audio), synth_calls=1,
            words_served=_wc(req.transcript), words_synthesized=_wc(req.transcript),
        )
        self._misses += 1
        return {"X-Cache": "MISS", "X-Cache-Key": key}, self._timed_chunks(_chunked(audio), t0, provider)

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
                        key, req, provider, model, params_canon,
                        audio, native_encoding, native_sample_rate, existing=record,
                    )
                await self._metrics.record_metrics(
                    provider=provider,
                    requests=1, misses=1, bytes_served=len(audio), synth_calls=1,
                    words_served=_wc(req.transcript), words_synthesized=_wc(req.transcript),
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
        self, req, key, fut, of, provider, instance, model, params_canon, record,
    ) -> tuple[dict, AsyncGenerator[bytes, None]]:
        """Serve a streaming MISS by awaiting an in-flight producer for ``key``.

        The producer (bytes- or stream-path) is already synthesizing; we wait for
        its result, then stream the completed clip in chunks (a coalesced HIT). If
        the producer aborted/failed, fall through to our own live synth — by the
        time ``await fut`` raises, the producer has cleared ``_inflight``, so we're
        the only contender. (CancelledError is NOT caught: a cancelled waiter dies.)
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
                    req, key, existing, of, provider, instance, model, params_canon, record
                )
            fut = self._new_inflight(key)
            return (
                {"X-Cache": "MISS", "X-Cache-Key": key},
                self._stream_and_store(
                    req, instance, key, provider, model, params_canon, record,
                    instance.native_encoding, instance.native_sample_rate, fut,
                ),
            )
        served = (
            native
            if _same_format(of.encoding, of.sample_rate, enc, rate)
            else await self._convert_audio(
                native, native_encoding=enc, native_rate=rate,
                out_encoding=of.encoding, out_rate=of.sample_rate,
            )
        )
        await self._metrics.touch_and_record(
            key, {"requests": 1, "hits": 1, "bytes_served": len(served),
                  "words_served": _wc(req.transcript)},
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
            key, req, provider, model, params_canon,
            audio, store_encoding, store_rate, existing=existing, replace=True,
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
        return key, status, source, len(audio), provider, model, store_encoding, store_rate

    async def warm_split(self, req: TTSRequest) -> int:
        """Predictive warm: cache the recurring phrase — and, when splitting is
        enabled, slice its timestamped audio into every contiguous sub-phrase.

        Sub-phrase splitting is opt-in (``predictive_warm_split_enabled``) and
        needs word boundaries (Cartesia only). When off (the default, matching
        the other providers) the recurring phrase is already cached whole by the
        request path, so this just ensures it exists (no wasted synth if it
        does). When on, one timestamped synth is sliced into the substring-closed
        set of entries stitch's binary-search assembly reads from; if the
        provider can't supply timestamps or boundaries don't align, it falls back
        to storing the whole phrase.

        Returns the number of (sub-)phrase entries stored.
        """
        # _resolve normalizes req.transcript (numbers -> words) in place and gives
        # the key. Derive norm_words AFTER so word-index slicing matches the
        # normalized text that gets synthesized (else the aligned-check breaks).
        provider, model, _of, params_canon, key = self._resolve(req)
        instance = self._get_provider(provider)
        if instance is None:
            raise ProviderNotConfigured(provider)

        norm_words = req.transcript.split()
        if not norm_words:
            return 0

        # No sub-phrase splitting (disabled, or provider has no timestamps): the
        # recurring phrase is almost always already cached from the request path,
        # so check first to avoid a wasted synth; store the whole phrase only if
        # it's genuinely new. The tracker still suppresses its sub-phrases.
        if not (
            settings.predictive_warm_split_enabled
            and hasattr(instance, "synth_with_timestamps")
        ):
            existing = await self._metadata.get(key)
            if existing and not self._expired(existing):
                return 0
            result = await self._synthesize(req, provider, model)
            await self._store(
                key, req, provider, model, params_canon,
                result.audio, "pcm_s16le", 16000, existing=existing,
            )
            await self._metrics.record_metrics(creates=1, synth_calls=1)
            logger.info(f"WARM (whole) key={key[:12]}… provider={provider} stored=1")
            return 1

        # Split path: synth once with word timestamps, slice into every
        # contiguous sub-phrase (check-before-store, so no re-synth). We synth
        # even if the full phrase is already cached: normal caching stores audio
        # WITHOUT word boundaries, so we need a timestamped synth to slice. The
        # tracker's warmed-set ensures one warm per phrase.
        aligned = False
        starts: list[float] = []
        gate = get_gate(provider)
        try:
            async with gate:
                audio, cwords, starts = await instance.synth_with_timestamps(
                    text=prepend_leading_dot(req.transcript, provider),
                    voice_id=req.voice.id, model=model,
                    language=req.language, params=req.params,
                )
            aligned = bool(cwords) and len(cwords) == len(norm_words)
        except Exception as e:
            logger.warning(f"timestamped synth failed for warm ({e}); full-phrase store")
            result = await self._synthesize(req, provider, model)
            audio = result.audio

        rate, bps = 16000, 2
        clip_end = len(audio) / (rate * bps)

        def byte_off(t: float) -> int:
            return int(round(t * rate * bps))

        # Sub-phrase word-index ranges to store: every contiguous slice if
        # aligned, else just the whole phrase.
        n = len(norm_words)
        ranges = (
            [(i, j) for i in range(n) for j in range(i + 1, n + 1)]
            if aligned
            else [(0, n)]
        )

        stored = 0
        for i, j in ranges:
            if aligned:
                end_t = starts[j] if j < len(starts) else clip_end
                slc = audio[byte_off(starts[i]) : byte_off(end_t)]
            else:
                slc = audio
            sub_text = " ".join(norm_words[i:j])
            sub_req = TTSRequest(
                model_id=req.model_id,
                transcript=sub_text,
                voice=req.voice,
                language=req.language,
                output_format=req.output_format,
                params=req.params,
            )
            sp, sm, _of, spc, skey = self._resolve(sub_req)
            sexisting = await self._metadata.get(skey)
            if sexisting and not self._expired(sexisting):
                continue  # already have this sub-phrase (e.g. from a prior split)
            await self._store(
                skey, sub_req, sp, sm, spc, slc, "pcm_s16le", rate, existing=sexisting
            )
            stored += 1

        await self._metrics.record_metrics(creates=stored, synth_calls=1)
        logger.info(
            f"WARM-SPLIT key={key[:12]}… provider={provider} words={n} "
            f"stored={stored} aligned={aligned}"
        )
        return stored

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

    async def clear(self, provider: str | None = None, voice_id: str | None = None) -> int:
        """Delete all entries (optionally filtered). Returns count removed.

        The store adjusts provider_totals atomically inside the DELETE
        transaction (SELECT+DELETE in one txn, so a concurrent insert can't
        escape the clear)."""
        deleted = await self._metadata.delete_filtered(provider=provider, voice_id=voice_id)
        for _prov, _size, path in deleted:
            await self._blobs.delete(path)
        if deleted:
            await self._metrics.record_metrics(deletes=len(deleted))
        logger.info(f"CLEAR removed {len(deleted)} entries (provider={provider}, voice_id={voice_id})")
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
        """Add hit_rate (+ words_from_cache_pct, stitch_coverage_avg when the
        raw sums exist) to a metrics map. Per-provider rows now carry
        words_synthesized, so words_from_cache_pct is derived for them too."""
        out = dict(m)
        req = m.get("requests", 0)
        out["hit_rate"] = round(m["hits"] / req, 4) if req else None
        wserved = m.get("words_served", 0)
        wsyn = m.get("words_synthesized")
        if wserved and wsyn is not None:
            # clamp at 0 — words_synthesized can transiently exceed words_served
            # (stitch/gap accounting) and a negative savings rate is nonsensical.
            out["words_from_cache_pct"] = max(0, int((wserved - wsyn) * 100 / wserved))
        assembled = (m.get("stitch_words_assembled", 0) or 0) + (
            m.get("stitch_words_synthesized", 0) or 0
        )
        if assembled:
            out["stitch_coverage_avg"] = round(
                (m.get("stitch_words_assembled", 0) or 0) / assembled, 4
            )
        return out

    async def daily_stats(
        self, from_date: str | None = None, to_date: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """Extensive day-wise metrics: per date, derived totals (hit_rate,
        words_from_cache_pct, stitch_coverage_avg on top of the raw sums) and a
        per-provider breakdown. ``provider`` narrows the view to one provider —
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
            days.append({
                "date": date,
                "totals": self._derive(base),
                "by_provider": {p: self._derive(m) for p, m in byp.items()},
                "latency": lat_by_day.get(date, {}),
            })
        return {"range": {"from": from_date, "to": to_date}, "days": days}

    async def _delete_rows(self, rows: list[dict], *, dry_run: bool, label: str) -> None:
        """Blob cleanup for a delete_where result (metadata already gone). Logs +
        continues on a per-blob failure so one bad unlink can't abort the batch."""
        if dry_run or not rows:
            return
        for r in rows:
            try:
                await self._blobs.delete(r["storage_path"])
            except Exception as e:
                logger.warning(f"{label}: blob delete failed for {r['storage_path']}: {e}")
        await self._metrics.record_metrics(deletes=len(rows))

    @staticmethod
    def _entries(rows: list[dict]) -> list[dict]:
        return [
            {"key": r["key"], "provider": r["provider"], "voice_id": r["voice_id"], "text": r["text"]}
            for r in rows
        ]

    async def delete_by_text(
        self, text: str | None = None, provider: str | None = None,
        voice_id: str | None = None, match: str = "exact", dry_run: bool = True,
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
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        rows = await self._metadata.delete_where("created_at < ?", [cutoff], dry_run=dry_run)
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
            "by_provider": {p: {"entries": c, "bytes": b} for p, (c, b) in by_prov.items()},
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
