"""ElevenLabs TTS provider adapter.

Ports clairvoyance's ``_generate_elevenlabs_audio`` helper into the uniform
``BaseTTSProvider`` contract. There is a single ``elevenlabs`` route, wired to
the Indian-residency endpoint (the only ElevenLabs account with API access).

Native output format: raw PCM 16 kHz (ElevenLabs ``output_format=pcm_16000``),
matching every other provider so a single cache entry serves all formats on read
and the streaming path live-forwards 16 kHz chunks to a 16 kHz caller. A
separate conversion layer (app/audio/format.py) maps this to the caller's
requested ``output_format`` (e.g. μ-law 8 kHz for telephony) before caching.

EXCEPTION — eleven_v3 models: the Text-to-Dialogue socket synthesizes at a
request-selected rate, and the v3-CONVERSATIONAL family carries DragonTTS-local
pipeline variants (see elevenlabs_pool.v3_conversational_variant; suffixes are
stripped before any upstream call):

- ``eleven_v3_conversational`` (base): ElevenLabs' own ``pcm_8000`` directly.
  tempo 1 serves it unaltered; tempo != 1 applies ONLY the pitch-preserving
  ffmpeg atempo stretch — no hygiene, no resample.
- ``eleven_v3_conversational_tempo``: full-band native rate (44.1 kHz in prod)
  + atempo, no hygiene. The custom anti-aliased sinc downsample to the
  caller's rate happens ONCE at synth time; the cache stores that end result.
- ``eleven_v3_conversational_clean_tempo``: full-band + utterance hygiene +
  atempo — the full processing chain.

For these models the cache layer keys on the requested output format and
stores the finished audio (see app/cache/service.py), so a cache hit is a
zero-processing byte serve.

Two paths:
- :meth:`synth` — one-shot HTTP ``/v1/text-to-speech/{voice}`` (used by
  ``/tts/bytes`` misses, stitch, and the warmer).
- :meth:`stream_synth` — the warm multi-context WebSocket pool
  (:mod:`app.providers.elevenlabs_pool`), low TTFB on ``/tts/stream`` misses.
  Falls back to one-shot HTTP if no warm socket is available.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx

from app.audio.atempo import (
    ATEMPO_MAX,
    ATEMPO_MIN,
    atempo_available,
    atempo_bytes,
    atempo_stream,
)
from app.audio.format import apply_presence_boost
from app.audio.hygiene import clean_utterance
from app.core.config import PROVIDER_DEFAULTS, settings
from app.core.logging import logger
from app.providers import elevenlabs_pool
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError
from app.providers.elevenlabs_pool import (
    is_elevenlabs_v3_conversational,
    is_elevenlabs_v3_model,
    normalize_v3_conversational,
    v3_conversational_variant,
)

# Models that accept a language_code (the multilingual flash/turbo variants).
# v3 (via the Text-to-Dialogue socket) also takes language_code and covers
# 70+ languages; multilingual_v2 auto-detects; English-only models ignore it.
# Mirrors pipecat's ELEVENLABS_MULTILINGUAL_MODELS.
_ELEVENLABS_MULTILINGUAL_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5"}


class ElevenLabsProvider(BaseTTSProvider):
    """ElevenLabs text-to-speech adapter.

    A single instance speaks to one ElevenLabs deployment (the Indian-residency
    endpoint); the registry wires the residency credentials to the
    ``elevenlabs`` route.
    """

    name = "elevenlabs"
    # Native PCM @ 16 kHz so the conversation path streams live and the cache
    # entry serves every requested format on read (μ-law/telephony is produced
    # by convert-on-serve, as for the other 16 kHz-native providers). v3 models
    # are the exception — see synth_native_format below.
    native_encoding = "pcm_s16le"
    native_sample_rate = 16000

    def synth_native_format(
        self, model: str | None = None, params: dict | None = None
    ) -> tuple[str, int]:
        """v3 (Text-to-Dialogue) synthesizes at the model-variant-selected rate
        (base v3conv = ElevenLabs' own pcm_8000; _tempo/_clean_tempo = the
        full-band native rate); classic at 16 kHz."""
        if is_elevenlabs_v3_model(model):
            return "pcm_s16le", self._v3_rate_for(model, params)
        return self.native_encoding, self.native_sample_rate

    def _v3_rate_for(self, model: str | None, params: dict | None) -> int:
        """Native rate for a v3 model given the request params.

        The v3-conversational model VARIANT selects the rate: the base model
        always speaks ElevenLabs' own ``pcm_8000`` (tempo 1 = served
        unaltered; tempo != 1 = atempo only), while ``_tempo`` /
        ``_clean_tempo`` generate at the configured full-band rate — the
        custom anti-aliased downsample to the caller's 8 kHz happens once, at
        synth time, and the cache stores that end result. Plain ``eleven_v3``
        (non-conversational) uses the configured native rate.
        """
        if (
            v3_conversational_variant(model) == "base"
            and settings.elevenlabs_tempo1_direct_pcm8000
        ):
            return 8000
        return settings.elevenlabs_v3_native_sample_rate

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """Initialize the provider.

        Args:
            api_key: ElevenLabs API key. Defaults to
                ``settings.elevenlabs_indian_residency_api_key``.
            base_url: ElevenLabs API base URL. Defaults to
                ``settings.elevenlabs_indian_residency_base_url`` (the residency
                host; the WS pool derives its wss host from this).
        """
        self.api_key = (
            api_key
            if api_key is not None
            else settings.elevenlabs_indian_residency_api_key
        )
        self.base_url = (
            base_url
            if base_url is not None
            else settings.elevenlabs_indian_residency_base_url
        )
        self._client = httpx.AsyncClient(timeout=30.0)
        # One warm pool per (voice_id, model_id, enable_ssml_parsing, language):
        # the WS binds voice (URL path) and model_id (connect-time query param)
        # for the socket's lifetime, and SSML + language are connect-time socket
        # settings (see _get_pool), so differing values need separate sockets.
        # Lazily created on first streaming miss; warmed eagerly when the model
        # matches a default.
        self._pools: dict[
            tuple[str, str, bool, str | None], elevenlabs_pool.ElevenLabsStreamPool
        ] = {}

    def _voice_settings(self, params: dict, model_id: str | None = None) -> dict:
        # Caller-supplied voice_settings win; otherwise build from the flat
        # tuning params. speed is ALWAYS set (explicit, else the DragonTTS
        # default) so an OMITTED speed and an EXPLICIT speed==default yield
        # identical audio — canonical_params collapses the latter to "absent",
        # so they must sound the same or one cache key would serve
        # different-speed audio.
        params = params or {}
        speed = params.get("speed")
        if speed is None:
            speed = PROVIDER_DEFAULTS.get("elevenlabs", {}).get("speed", 1.0)
        vs = params.get("voice_settings")
        if isinstance(vs, dict) and vs:
            vs = dict(vs)
            vs.setdefault("speed", speed)
        else:
            vs = {"speed": speed}
            for key in ("stability", "similarity_boost"):
                value = params.get(key)
                if value is not None:
                    vs[key] = value
        if is_elevenlabs_v3_model(model_id):
            # Text-to-Dialogue reads ONLY stability; sending speed /
            # similarity_boost would imply an effect the endpoint doesn't have
            # (they're silently ignored). Keep stability when present.
            dropped = sorted(k for k in vs if k != "stability")
            if dropped:
                logger.info(
                    f"ElevenLabs v3 model {model_id}: dropping unsupported "
                    f"voice_settings {dropped} (Text-to-Dialogue reads only stability)"
                )
            vs = {k: v for k, v in vs.items() if k == "stability"}
        logger.info(f"ElevenLabs voice_settings: {vs}")
        return vs

    def _tempo_for(self, params: dict | None, model_id: str | None) -> float:
        """Effective ffmpeg atempo factor for this request (1.0 = bypass).

        Only ``eleven_v3_conversational`` is ever stretched — every other model
        returns 1.0 regardless of params (the cache layer also strips tempo for
        non-v3conv models, so their keys never fragment). Exactly 1.0 — absent,
        default, or explicit — never spawns ffmpeg.
        """
        if not is_elevenlabs_v3_conversational(model_id):
            return 1.0
        if not settings.elevenlabs_atempo_enabled:
            return 1.0
        raw = (params or {}).get("tempo")
        if raw is None:
            raw = settings.elevenlabs_v3conv_default_tempo
        try:
            tempo = float(raw)
        except (TypeError, ValueError):
            logger.warning(f"ElevenLabs tempo {raw!r} is not numeric — using 1.0")
            return 1.0
        if tempo == 1.0:
            return 1.0
        if not ATEMPO_MIN <= tempo <= ATEMPO_MAX:
            # The schema validator 400s out-of-range request tempo before we
            # get here; reaching this means a bad knob default — degrade, don't fail.
            logger.warning(
                f"ElevenLabs tempo {tempo} outside [{ATEMPO_MIN}, {ATEMPO_MAX}] "
                f"— using 1.0"
            )
            return 1.0
        if not atempo_available():
            logger.error(
                "tempo requested but ffmpeg is not on PATH — passing audio "
                "through unstretched (install ffmpeg, or silence with "
                "ELEVENLABS_ATEMPO_ENABLED=0)"
            )
            return 1.0
        logger.info(
            f"ElevenLabs atempo: applying tempo={tempo} to {model_id} "
            f"(pitch-preserving ffmpeg stretch; cached post-stretch)"
        )
        return tempo

    def _get_pool(
        self,
        voice_id: str,
        model_id: str,
        enable_ssml_parsing: bool = False,
        language: str | None = None,
        sample_rate: int | None = None,
    ) -> elevenlabs_pool.ElevenLabsStreamPool | None:
        """Return the warm pool for (voice, model, ssml, language, rate), creating it lazily.

        Returns ``None`` when pooling is disabled (pool size 0) or the key is
        missing, so the caller falls back to one-shot synth. NB: ``_pools`` grows
        with distinct (voice, model, ssml, language) tuples and is only cleared
        at shutdown — acceptable while the voice catalog stays fixed (re-add an
        LRU cap if it ever diversifies). SSML and language are part of the key
        because they're connect-time socket settings: an SSML-on socket parses
        <break/> tags for ALL its utterances, and language_code pins the socket's
        language, so differing values can't share one socket. ``sample_rate``
        (v3 only) is likewise connect-time: tempo-1 v3conv requests synthesize
        ElevenLabs' own pcm_8000 while stretched ones use the full-band rate,
        so they live on separate warm sockets.
        """
        if not self.api_key:
            return None
        # v3 (Text-to-Dialogue) pools are sized by their own knob: TTD sockets
        # hold a permanent keepalive context and eleven_v3 has NO HTTP
        # fallback, so they warm independently of the classic pool size.
        pool_size = (
            settings.elevenlabs_dialogue_pool_size
            if is_elevenlabs_v3_model(model_id)
            else settings.elevenlabs_stream_pool_size
        )
        if pool_size < 1:
            return None
        # Non-multilingual models ignore language on the socket (the pool only
        # sends language_code for multilingual models), so normalize it out of
        # the key — otherwise identical requests that differ only by language
        # each spin up a redundant warm socket (pool fragmentation). v3 models
        # DO take a connect-time language_code, so their key keeps it.
        if (
            model_id not in _ELEVENLABS_MULTILINGUAL_MODELS
            and not is_elevenlabs_v3_model(model_id)
        ):
            language = None
        # output_format is connect-time, so the v3 rate rides in the key.
        v3_rate = (
            sample_rate
            if sample_rate is not None and is_elevenlabs_v3_model(model_id)
            else None
        )
        key = (voice_id, model_id, enable_ssml_parsing, language, v3_rate)
        pool = self._pools.get(key)
        if pool is None:
            pool = elevenlabs_pool.ElevenLabsStreamPool(
                api_key=self.api_key,
                voice_id=voice_id,
                model_id=model_id,
                base_url=self.base_url,
                idle_timeout=settings.elevenlabs_stream_idle_timeout,
                min_size=pool_size,
                max_size=max(pool_size * 2, pool_size + 4),
                enable_ssml_parsing=enable_ssml_parsing,
                language=language,
                # v3 sockets synthesize at the request-selected rate (tempo-1
                # v3conv = ElevenLabs' own pcm_8000, direct and unaltered;
                # stretched = the full-band native rate); classic sockets keep
                # the 16 kHz cache-native rate.
                output_format=(
                    f"pcm_{v3_rate or settings.elevenlabs_v3_native_sample_rate}"
                    if is_elevenlabs_v3_model(model_id)
                    else "pcm_16000"
                ),
            )
            self._pools[key] = pool
        return pool

    async def warm(self) -> None:
        """Pre-warm the pool for the default voice+model (called at startup).

        Other (voice, model) combos warm lazily on their first streaming miss.
        """
        if not self.api_key or settings.elevenlabs_stream_pool_size < 1:
            return
        defaults = PROVIDER_DEFAULTS.get("elevenlabs", {})
        voice = defaults.get("voice_id", "")
        model = defaults.get("model", "eleven_flash_v2_5")
        language = defaults.get("language")
        if not voice or not model:
            return
        try:
            pool = self._get_pool(voice, model, language=language)
            if pool is not None:
                await pool.start()
        except Exception as e:
            logger.warning(f"ElevenLabs stream pool warm failed: {e}")

    async def aclose(self) -> None:
        await self._client.aclose()
        for pool in self._pools.values():
            await pool.aclose()
        self._pools.clear()

    async def synth(
        self,
        *,
        text: str,
        voice_id: str,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AudioResult:
        """Synthesize ``text`` and return raw PCM 16 kHz audio.

        Uses the one-shot ``/v1/text-to-speech/{voice_id}`` HTTP endpoint with
        ``output_format=pcm_16000``.

        Args:
            text: The text to synthesize.
            voice_id: ElevenLabs voice ID. Falls back to
                ``PROVIDER_DEFAULTS["elevenlabs"]["voice_id"]`` when empty.
            model: ElevenLabs model ID. Falls back to the provider default when
                empty.
            language: BCP-47 language hint. Falls back to the provider default
                when empty.
            params: Extra provider-specific options. ``voice_settings`` is
                honored if present.

        Returns:
            AudioResult wrapping the provider's native ``pcm_16000`` bytes.

        Raises:
            ValueError: If ``self.api_key`` is missing.
        """
        if not self.api_key:
            raise ValueError("ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required")

        defaults = PROVIDER_DEFAULTS["elevenlabs"]
        final_voice_id = voice_id if voice_id else defaults["voice_id"]
        final_model_id = model if model else defaults["model"]
        final_language = language if language else defaults["language"]

        if is_elevenlabs_v3_model(final_model_id):
            # Eleven v3 exists ONLY on the Text-to-Dialogue socket — the
            # classic /v1/text-to-speech endpoint below returns 404 for it.
            return await self._synth_v3(
                text=text,
                voice_id=final_voice_id,
                model=final_model_id,
                language=final_language,
                params=params,
            )

        url = f"{self.base_url}/v1/text-to-speech/{final_voice_id}?output_format=pcm_16000"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/raw",
        }
        payload = {
            "text": text,
            "model_id": final_model_id,
            "voice_settings": self._voice_settings(params, final_model_id),
        }
        # language_code: the multilingual models (flash_v2_5/turbo_v2_5) and
        # the v3 models accept it; multilingual_v2 auto-detects, English-only
        # models ignore it. Send the base subtag ("en"/"hi") — matches
        # pipecat's use_base_code.
        if (
            final_model_id in _ELEVENLABS_MULTILINGUAL_MODELS
            or is_elevenlabs_v3_model(final_model_id)
        ) and final_language:
            payload["language_code"] = final_language.split("-")[0]
        if params.get("enable_ssml_parsing"):
            # SSML on: ElevenLabs parses <break time=".."/> etc. into real
            # pauses instead of reading the tags aloud. Default off; only sent
            # when requested. v3 does not support SSML parsing — silently
            # reading the tags aloud would corrupt v3 audio, so the flag is
            # dropped for v3 models instead of forwarded.
            if is_elevenlabs_v3_model(final_model_id):
                logger.warning(
                    "enable_ssml_parsing requested with an eleven_v3 model — "
                    "not supported on v3; ignoring"
                )
            else:
                payload["enable_ssml_parsing"] = True

        logger.info(
            f"Synthesizing with ElevenLabs (pcm_16000): {text[:50]}... "
            f"[voice_id={final_voice_id}, model_id={final_model_id}, "
            f"language={final_language}, base_url={self.base_url}]"
        )

        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return AudioResult(
            audio=response.content,
            container="raw",
            encoding="pcm_s16le",
            sample_rate=16000,
        )

    async def _synth_v3(
        self,
        *,
        text: str,
        voice_id: str,
        model: str,
        language: str | None,
        params: dict,
    ) -> AudioResult:
        """One-shot eleven_v3 synth: one Text-to-Dialogue utterance, joined.

        The bytes path (/tts/bytes, warmer) previously fell through to the
        classic HTTP endpoint, which 404s for v3 — v3 is TTD-only. Speak the
        utterance over the warm TTD pool (the same socket stream_synth uses)
        and accumulate the clip. There is no HTTP fallback, so a pool failure
        propagates (the API layer maps it to 502/503).
        """
        if params.get("enable_ssml_parsing"):
            logger.warning(
                "enable_ssml_parsing requested with an eleven_v3 model — "
                "not supported on v3; ignoring"
            )
        lang_code = language.split("-")[0] if language else None
        variant = v3_conversational_variant(model)
        tempo = self._tempo_for(params, model)
        sample_rate = self._v3_rate_for(model, params)
        # The variant suffix is DragonTTS-local; ElevenLabs only knows the
        # base id, so every upstream call speaks the normalized name.
        upstream_model = normalize_v3_conversational(model)
        pool = self._get_pool(voice_id, upstream_model, False, lang_code, sample_rate)
        if pool is None:
            raise ProviderError(
                "ElevenLabs v3 requires the stream pool "
                "(ELEVENLABS_DIALOGUE_POOL_SIZE >= 1) — Text-to-Dialogue has "
                "no HTTP endpoint"
            )
        direct = variant == "base" and sample_rate == 8000 and tempo == 1.0
        logger.info(
            f"Synthesizing with ElevenLabs Text-to-Dialogue (pcm_{sample_rate})"
            f"{' — direct, unaltered' if direct else ''}"
            f"{'' if variant in (None, 'base') else f' [{variant}]'}"
            f"{f' [tempo={tempo}]' if tempo != 1.0 else ''}: "
            f"{text[:50]}... [voice_id={voice_id}, model_id={model}]"
        )
        msg = {
            "text": text,
            "voice_settings": self._voice_settings(params, upstream_model),
        }
        chunks = []
        async for chunk in pool.stream(msg):
            chunks.append(chunk)
        audio = b"".join(chunks)
        if direct:
            # base model, speed 1: serve ElevenLabs' own pcm_8000 exactly as
            # generated — no hygiene, no stretch, no downsample.
            return AudioResult(
                audio=audio,
                container="raw",
                encoding="pcm_s16le",
                sample_rate=8000,
            )
        # Hygiene runs ONLY where the variant asks for clean: _clean_tempo,
        # plain eleven_v3 (legacy chain), or the base model when its direct-8k
        # path is knob-disabled (then it runs the full-band chain instead).
        # base and _tempo at tempo != 1 stay untouched except for atempo.
        if settings.elevenlabs_utterance_hygiene_enabled and (
            variant in ("clean_tempo", None)
            or (variant == "base" and sample_rate != 8000)
        ):
            cleaned = clean_utterance(
                audio,
                sample_rate,
                lead_ms=settings.elevenlabs_hygiene_lead_ms,
                tail_ms=settings.elevenlabs_hygiene_tail_ms,
                max_pause_ms=settings.elevenlabs_hygiene_max_pause_ms,
                gate_floor=settings.elevenlabs_hygiene_gate_floor,
                content_factor=settings.elevenlabs_hygiene_content_factor,
                content_abs=settings.elevenlabs_hygiene_content_abs_floor,
            )
            if len(cleaned) != len(audio):
                logger.info(
                    f"ElevenLabs hygiene: {len(audio) / 2 / sample_rate:.2f}s -> "
                    f"{len(cleaned) / 2 / sample_rate:.2f}s (trimmed sub-floor "
                    f"pads, capped sub-floor pauses, gated noise)"
                )
            audio = cleaned
        # v3_conversational only: tempo 1.0 never spawns ffmpeg. On a stretch
        # failure the unstretched clip is served AND cached under the
        # tempo-keyed entry — consistent while ffmpeg is broken, and the error
        # log points at the fix.
        if tempo != 1.0:
            try:
                audio = await atempo_bytes(audio, sample_rate, tempo)
            except Exception as exc:
                logger.error(
                    f"atempo batch stretch failed (tempo={tempo}) — "
                    f"serving/caching unstretched: {exc}"
                )
        # Spark recovery for the full-band variants: band-limiting to 8 kHz
        # deletes everything above ~3.4 kHz, so the top surviving octave gets
        # a gentle presence re-weighting BEFORE the downsample (see
        # apply_presence_boost). Off by default; never touches the base model.
        boost = settings.elevenlabs_telephony_presence_boost_db
        if variant in ("tempo", "clean_tempo") and abs(boost) >= 0.05:
            audio = apply_presence_boost(audio, sample_rate, boost)
        return AudioResult(
            audio=audio,
            container="raw",
            encoding="pcm_s16le",
            sample_rate=sample_rate,
        )

    async def stream_synth(
        self,
        *,
        text: str,
        voice_id: str,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AsyncGenerator[bytes, None]:
        """Stream native PCM 16 kHz chunks via the warm multi-context WS pool.

        Each utterance is sent on a warm socket (one handshake amortized across
        many misses); ``is_final`` ends the stream and the context is closed. If
        no warm socket is ready (pool disabled, circuit open, or cold start) and
        nothing has been streamed yet, fall back to one-shot HTTP synth so the
        miss still completes.

        Raises:
            ProviderError: If the API key is missing, or the socket fails after
                audio has started streaming (can't safely fall back mid-stream).
        """
        if not self.api_key:
            raise ProviderError("ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required")

        defaults = PROVIDER_DEFAULTS["elevenlabs"]
        final_voice_id = voice_id if voice_id else defaults["voice_id"]
        final_model_id = model if model else defaults["model"]
        final_language = language if language else defaults["language"]
        # Variant suffixes (_tempo/_clean_tempo) are DragonTTS-local pipeline
        # selectors — ElevenLabs only knows the base id.
        upstream_model_id = normalize_v3_conversational(final_model_id)

        msg = {
            "text": text,
            "voice_settings": self._voice_settings(params, upstream_model_id),
        }
        # SSML is a connect-time socket setting (see _get_pool / pool URI), so it
        # selects which warm pool to use — not a per-message field. v3 never
        # takes it (Text-to-Dialogue has no SSML support), so it's normalized
        # out of the key — keeping it would split the v3 pool in two for no
        # effect, and the bytes path (_synth_v3) always passes False.
        ssml = bool(params.get("enable_ssml_parsing")) and not is_elevenlabs_v3_model(
            final_model_id
        )
        logger.info(
            f"Streaming via ElevenLabs multi-context WS: {text[:50]}... "
            f"[voice_id={final_voice_id}, model_id={final_model_id}, ssml={ssml}]"
        )

        lang_code = final_language.split("-")[0] if final_language else None
        pool = self._get_pool(
            final_voice_id,
            upstream_model_id,
            ssml,
            lang_code,
            self._v3_rate_for(final_model_id, params),
        )
        if pool is not None:
            streamed_any = False
            try:
                source = pool.stream(msg)
                # v3_conversational only: wrap the pool stream through a
                # persistent ffmpeg atempo process. Stretched chunks are what
                # the caller forwards AND what cache-during-stream accumulates
                # — identical bytes to the batch path. tempo 1.0 skips the
                # wrap entirely; provider errors propagate through the wrapper
                # unchanged so the fallbacks below still work.
                tempo = self._tempo_for(params, final_model_id)
                if tempo != 1.0:
                    source = atempo_stream(
                        source, self._v3_rate_for(final_model_id, params), tempo
                    )
                async for chunk in source:
                    streamed_any = True
                    yield chunk
                return
            except elevenlabs_pool.SocketUnavailable:
                # WS pool unreachable (cold start, blocked handshake, circuit
                # open). Only fall back if we haven't streamed partial audio.
                if streamed_any:
                    raise
                logger.warning(
                    "ElevenLabs WS unavailable — serving miss via one-shot HTTP synth"
                )
            except Exception as e:
                # WS connected but the utterance failed (schema/quota/error frame).
                # Only fall back if we haven't streamed partial audio yet.
                if streamed_any:
                    raise
                logger.warning(
                    f"ElevenLabs WS stream failed ({e}) — "
                    f"serving miss via one-shot HTTP synth"
                )

        # Fallback: one-shot HTTP synth (also used when pooling is disabled).
        result = await self.synth(
            text=text,
            voice_id=final_voice_id,
            model=final_model_id,
            language=final_language,
            params=params,
        )
        yield result.audio
