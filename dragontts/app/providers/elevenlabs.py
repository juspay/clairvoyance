"""ElevenLabs TTS provider adapter.

Ports clairvoyance's ``_generate_elevenlabs_audio`` helper into the uniform
``BaseTTSProvider`` contract. There is a single ``elevenlabs`` route, wired to
the Indian-residency endpoint (the only ElevenLabs account with API access).

Native output format: raw PCM 16 kHz (ElevenLabs ``output_format=pcm_16000``),
matching every other provider so a single cache entry serves all formats on read
and the streaming path live-forwards 16 kHz chunks to a 16 kHz caller. A
separate conversion layer (app/audio/format.py) maps this to the caller's
requested ``output_format`` (e.g. μ-law 8 kHz for telephony) before caching.

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

from app.core.config import PROVIDER_DEFAULTS, settings
from app.core.logging import logger
from app.providers import elevenlabs_pool
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError


class ElevenLabsProvider(BaseTTSProvider):
    """ElevenLabs text-to-speech adapter.

    A single instance speaks to one ElevenLabs deployment (the Indian-residency
    endpoint); the registry wires the residency credentials to the
    ``elevenlabs`` route.
    """

    name = "elevenlabs"
    # Native PCM @ 16 kHz so the conversation path streams live and the cache
    # entry serves every requested format on read (μ-law/telephony is produced
    # by convert-on-serve, as for the other 16 kHz-native providers).
    native_encoding = "pcm_s16le"
    native_sample_rate = 16000

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
        # One warm pool per (voice_id, model_id): the WS binds voice (URL path)
        # and model_id (connect-time query param) for the socket's lifetime, so
        # differing voice/model need separate sockets. Lazily created on first
        # streaming miss; warmed eagerly when the model matches a default.
        self._pools: dict[tuple[str, str], elevenlabs_pool.ElevenLabsStreamPool] = {}

    def _voice_settings(self, params: dict) -> dict:
        # Caller-supplied voice_settings win; otherwise mirror the one-shot
        # defaults. speed is ALWAYS set (explicit, else the DragonTTS default) so
        # an OMITTED speed and an EXPLICIT speed==default yield identical audio —
        # canonical_params collapses the latter to "absent", so they must sound
        # the same or one cache key would serve different-speed audio.
        speed = (params or {}).get("speed")
        if speed is None:
            speed = PROVIDER_DEFAULTS.get("elevenlabs", {}).get("speed", 1.0)
        vs = (params or {}).get("voice_settings")
        if isinstance(vs, dict) and vs:
            vs = dict(vs)
            vs.setdefault("speed", speed)
            return vs
        return {"stability": 0.5, "similarity_boost": 0.75, "speed": speed}

    def _get_pool(
        self, voice_id: str, model_id: str, enable_ssml_parsing: bool = False
    ) -> elevenlabs_pool.ElevenLabsStreamPool | None:
        """Return the warm pool for (voice, model, ssml), creating it lazily.

        Returns ``None`` when pooling is disabled (pool size 0) or the key is
        missing, so the caller falls back to one-shot synth. NB: ``_pools`` grows
        with distinct (voice, model, ssml) triples and is only cleared at
        shutdown — acceptable while the voice catalog stays fixed (re-add an LRU
        cap if it ever diversifies). SSML is part of the key because it's a
        connect-time socket setting: an SSML-on socket parses <break/> tags for
        ALL its utterances, so on/off can't share one socket.
        """
        if not self.api_key or settings.elevenlabs_stream_pool_size < 1:
            return None
        key = (voice_id, model_id, enable_ssml_parsing)
        pool = self._pools.get(key)
        if pool is None:
            pool = elevenlabs_pool.ElevenLabsStreamPool(
                api_key=self.api_key,
                voice_id=voice_id,
                model_id=model_id,
                base_url=self.base_url,
                idle_timeout=settings.elevenlabs_stream_idle_timeout,
                min_size=settings.elevenlabs_stream_pool_size,
                max_size=max(
                    settings.elevenlabs_stream_pool_size * 2,
                    settings.elevenlabs_stream_pool_size + 4,
                ),
                enable_ssml_parsing=enable_ssml_parsing,
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
        if not voice or not model:
            return
        try:
            pool = self._get_pool(voice, model)
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

        url = f"{self.base_url}/v1/text-to-speech/{final_voice_id}?output_format=pcm_16000"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/raw",
        }
        # NOTE: language_code is intentionally NOT sent — the ElevenLabs
        # /v1/text-to-speech/{voice_id} body has no such field (language is
        # bound to the model), and clairvoyance's reference doesn't send it.
        payload = {
            "text": text,
            "model_id": final_model_id,
            "voice_settings": self._voice_settings(params),
        }
        if params.get("enable_ssml_parsing"):
            # SSML on: ElevenLabs parses <break time=".."/> etc. into real
            # pauses instead of reading the tags aloud. Default off; only sent
            # when requested (all models except eleven_v3 support it).
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

        msg = {"text": text, "voice_settings": self._voice_settings(params)}
        # SSML is a connect-time socket setting (see _get_pool / pool URI), so it
        # selects which warm pool to use — not a per-message field.
        ssml = bool(params.get("enable_ssml_parsing"))
        logger.info(
            f"Streaming via ElevenLabs multi-context WS: {text[:50]}... "
            f"[voice_id={final_voice_id}, model_id={final_model_id}, ssml={ssml}]"
        )

        pool = self._get_pool(final_voice_id, final_model_id, ssml)
        if pool is not None:
            streamed_any = False
            try:
                async for chunk in pool.stream(msg):
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
