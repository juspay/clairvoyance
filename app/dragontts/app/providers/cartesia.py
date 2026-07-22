"""Cartesia TTS provider adapter.

Ports clairvoyance's ``_generate_cartesia_audio`` helper into the uniform
``BaseTTSProvider.synth`` contract. On a cache miss DragonTTS delegates raw
synthesis here, then a separate conversion layer maps the native format to the
caller's requested ``output_format`` before caching.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncGenerator

import httpx

from app.core.config import PROVIDER_DEFAULTS, settings
from app.core.logging import logger
from app.providers import cartesia_pool
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError

# Cartesia bytes endpoint + protocol version
# (mirrors clairvoyance / pipecat).
_CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_VERSION = "2024-06-10"


class CartesiaProvider(BaseTTSProvider):
    """Cartesia TTS backend.

    Returns audio in Cartesia's native raw PCM format (16-bit signed LE,
    16 kHz mono). ``synth`` uses the one-shot ``/tts/bytes`` endpoint;
    ``stream_synth`` uses the streaming ``/tts/websocket`` to emit chunks as
    they are synthesized (low TTFB on a cache miss).
    """

    name = "cartesia"

    @staticmethod
    def _generation_config(params: dict) -> dict:
        """Cartesia applies speed/volume/emotion via a top-level generation_config
        (matches pipecat's CartesiaTTSService). Only non-None values are sent;
        these are part of the cache key upstream."""
        return {
            k: params[k]
            for k in ("speed", "volume", "emotion")
            if params.get(k) is not None
        }

    def __init__(self, api_key: str | None = None):
        """Initialize the provider.

        Args:
            api_key: Cartesia API key. Defaults to ``settings.cartesia_api_key``
                when ``None``.
        """
        self.api_key = api_key or settings.cartesia_api_key
        self._client = httpx.AsyncClient(timeout=30.0)  # pooled, reused across synths
        # Warm pool of streaming sockets, created lazily and warmed at startup.
        self._pool: cartesia_pool.CartesiaStreamPool | None = None

    def _get_pool(self) -> cartesia_pool.CartesiaStreamPool:
        if self._pool is None:
            self._pool = cartesia_pool.CartesiaStreamPool(
                self.api_key, min_size=settings.cartesia_stream_pool_size
            )
        return self._pool

    async def warm(self) -> None:
        """Pre-warm the streaming socket pool (called at app startup)."""
        if self.api_key and settings.cartesia_stream_pool_size >= 1:
            try:
                await self._get_pool().start()
            except Exception as e:
                logger.warning(f"Cartesia stream pool warm failed: {e}")

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._pool is not None:
            await self._pool.aclose()

    async def synth(
        self,
        *,
        text: str,
        voice_id: str | None,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AudioResult:
        """Synthesize ``text`` and return native-format raw PCM audio.

        Fields missing from the request fall back to
        ``PROVIDER_DEFAULTS["cartesia"]``.

        Args:
            text: The text to synthesize.
            voice_id: Voice ID override, or ``None``/empty for the default.
            model: Model override, or ``None``/empty for the default.
            language: Language override, or ``None``/empty for the default.
            params: Reserved for future provider-specific options (phase 1:
                typically empty).

        Returns:
            AudioResult holding raw ``pcm_s16le`` audio at 16 kHz.

        Raises:
            ValueError: If the Cartesia API key is not configured.
        """
        if not self.api_key:
            raise ValueError("CARTESIA_API_KEY is required")

        defaults = PROVIDER_DEFAULTS["cartesia"]
        final_voice_id = voice_id or defaults.get("voice_id", "")
        final_model = model or defaults.get("model", "sonic-3.5")
        final_language = language or defaults.get("language", "en")

        headers = {
            "X-API-Key": self.api_key,
            "Cartesia-Version": _CARTESIA_VERSION,
            "Content-Type": "application/json",
        }

        # Cartesia applies speed/volume/emotion via a top-level generation_config
        # (matches pipecat's CartesiaTTSService, model_dump(exclude_none=True)).
        generation_config = self._generation_config(params)

        payload = {
            "model_id": final_model,
            "transcript": text,
            "voice": {"mode": "id", "id": final_voice_id},
            "language": final_language,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": 16000,
            },
        }
        if generation_config:
            payload["generation_config"] = generation_config

        logger.info(
            f"Synthesizing audio with Cartesia: {text[:50]}... "
            f"[voice_id={final_voice_id}, model={final_model}]"
        )

        response = await self._client.post(_CARTESIA_URL, json=payload, headers=headers)
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
        voice_id: str | None,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AsyncGenerator[bytes, None]:
        """Stream native-format PCM chunks from the warm Cartesia socket pool.

        Yields raw ``pcm_s16le`` @ 16 kHz chunks as Cartesia synthesizes them,
        so a cache miss reaches the caller with low TTFB. Sockets are reused
        across utterances, so only the first miss (or one after an idle drop)
        pays the WSS handshake. The caller accumulates and stores the full clip
        on completion.

        Raises:
            ProviderError: If the API key is missing, Cartesia reports an error,
                the socket fails, or the stream ends without completion.
        """
        if not self.api_key:
            raise ProviderError("CARTESIA_API_KEY is required")

        defaults = PROVIDER_DEFAULTS["cartesia"]
        final_voice_id = voice_id or defaults.get("voice_id", "")
        final_model = model or defaults.get("model", "sonic-3.5")
        final_language = language or defaults.get("language", "en")

        generation_config = self._generation_config(params)

        # ``continue: false`` => the transcript is complete; Cartesia synthesizes
        # the whole utterance and signals the end with a ``done`` message. The
        # pool injects a unique ``context_id`` for routing/multiplexing.
        msg = {
            "transcript": text,
            "continue": False,
            "model_id": final_model,
            "voice": {"mode": "id", "id": final_voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": 16000,
            },
            "add_timestamps": False,
            "language": final_language,
        }
        if generation_config:
            msg["generation_config"] = generation_config

        logger.info(
            f"Streaming audio via Cartesia: {text[:50]}... "
            f"[voice_id={final_voice_id}, model={final_model}]"
        )

        if settings.cartesia_stream_pool_size >= 1:
            streamed_any = False
            try:
                async for chunk in self._get_pool().stream(msg):
                    streamed_any = True
                    yield chunk
                return
            except cartesia_pool.SocketUnavailable:
                # WS pool unreachable (e.g. blocked handshake, circuit open).
                # Only fall back if we haven't streamed partial audio yet.
                if streamed_any:
                    raise
                logger.warning(
                    "Cartesia WS unavailable — serving miss via one-shot HTTP synth"
                )

        # Fallback: one-shot HTTP synth (also used when pooling is disabled or
        # the WS pool is unreachable). Yields the full clip; the cache layer
        # accumulates and stores it as usual.
        result = await self.synth(
            text=text, voice_id=voice_id, model=model, language=language, params=params
        )
        yield result.audio

    async def synth_with_timestamps(
        self,
        *,
        text: str,
        voice_id: str | None,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> tuple[bytes, list[str], list[float]]:
        """One-shot synth returning native PCM + Cartesia's per-word boundaries.

        Used by the predictive warmer to split the phrase's audio into sub-phrase
        cache entries (1 Cartesia call per phrase). Opens a transient WS with
        ``add_timestamps: true`` (warming is background, so no pool needed) and
        reuses the IPv4-forcing connector. Cartesia streams timestamps in several
        batched messages, so words/starts are accumulated across all of them.

        Returns:
            (audio_bytes, words, starts) where starts[i] is the start time (s) of
            words[i]; word i spans [starts[i], starts[i+1]) and the last word spans
            [starts[-1], clip_end).
        """
        if not self.api_key:
            raise ProviderError("CARTESIA_API_KEY is required")

        defaults = PROVIDER_DEFAULTS["cartesia"]
        final_voice_id = voice_id or defaults.get("voice_id", "")
        final_model = model or defaults.get("model", "sonic-3.5")
        final_language = language or defaults.get("language", "en")
        generation_config = self._generation_config(params)

        msg = {
            "transcript": text,
            "continue": False,
            "model_id": final_model,
            "voice": {"mode": "id", "id": final_voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": 16000,
            },
            "add_timestamps": True,
            "language": final_language,
        }
        if generation_config:
            msg["generation_config"] = generation_config

        uri = (
            f"{cartesia_pool._CARTESIA_WS_URL}?api_key={self.api_key}"
            f"&cartesia_version={_CARTESIA_VERSION}"
        )
        ctx_id = uuid.uuid4().hex
        audio = bytearray()
        words: list[str] = []
        starts: list[float] = []

        logger.info(
            f"Synthesizing (with timestamps) via Cartesia: {text[:50]}... "
            f"[voice_id={final_voice_id}, model={final_model}]"
        )
        async with cartesia_pool._default_connect(uri) as ws:
            await ws.send(json.dumps({**msg, "context_id": ctx_id}))
            async for message in ws:
                try:
                    m = json.loads(message)
                except (ValueError, TypeError):
                    continue
                if m.get("context_id") != ctx_id:
                    continue
                msg_type = m.get("type")
                if msg_type == "chunk":
                    audio += base64.b64decode(m["data"])
                elif msg_type == "timestamps":
                    wt = m["word_timestamps"]
                    words.extend(wt["words"])
                    starts.extend(wt["start"])
                elif msg_type == "done":
                    break
                elif msg_type == "error":
                    raise ProviderError(f"cartesia timestamped synth error: {m}")

        if not audio or not starts:
            raise ProviderError("cartesia timestamped synth returned no audio/timestamps")
        return bytes(audio), words, starts

