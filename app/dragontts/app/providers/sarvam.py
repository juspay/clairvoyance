"""Sarvam TTS provider adapter.

Ports clairvoyance's ``_generate_sarvam_audio`` helper into the uniform
:class:`BaseTTSProvider` contract. Native output is raw 16-bit PCM at 16 kHz:

- :meth:`synth` — one-shot HTTP ``/text-to-speech`` requesting
  ``speech_sample_rate=16000`` (16 kHz PCM directly, base64 in the JSON body).
- :meth:`stream_synth` — the warm Sarvam WS pool
  (:mod:`app.providers.sarvam_pool`). Sarvam streams at the model's native rate
  (bulbul:v2 = 22050 Hz, bulbul:v3 = 24000 Hz), so each chunk is fed through a
  stateful :class:`~app.audio.resample.StreamingResampler` to emit 16 kHz chunks
  (low TTFB on a ``/tts/stream`` miss). Falls back to one-shot HTTP if no warm
  socket is available. Both paths converge on 16 kHz PCM, so a single cache
  entry serves every requested format on read.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator

import httpx

from app.audio.resample import StreamingResampler
from app.core.config import PROVIDER_DEFAULTS, settings
from app.core.logging import logger
from app.providers import sarvam_pool
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError

# Sarvam always returns 16-bit PCM; the one-shot HTTP path requests 16 kHz.
_SARVAM_SAMPLE_RATE = 16000


def _is_v3(model: str) -> bool:
    """bulbul:v3 rejects pitch/loudness (only pace + temperature); v2 accepts them."""
    return model.strip().lower().endswith("v3")


class SarvamProvider(BaseTTSProvider):
    """Adapter for the Sarvam text-to-speech HTTP + WebSocket API."""

    name = "sarvam"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.sarvam_api_key
        self._client = httpx.AsyncClient(timeout=30.0)
        # One warm WS pool per model: the model is a connect-time query param, so
        # differing models need separate sockets. Lazily created on first miss.
        self._pools: dict[str, sarvam_pool.SarvamStreamPool] = {}

    def _resolve(self, *, voice_id: str, model: str | None, language: str | None, params: dict):
        defaults = PROVIDER_DEFAULTS["sarvam"]
        voice = voice_id or defaults.get("voice_id", "shreya")
        mdl = model or defaults.get("model", "bulbul:v3")
        language_code = language or defaults.get("language", "en-IN")
        params = params or {}
        pitch = params.get("pitch")
        if pitch is None:
            pitch = defaults.get("pitch", 0.0)
        pace = params.get("pace")
        if pace is None:
            pace = params.get("speed")
        if pace is None:
            pace = defaults.get("speed", 0.9)
        return voice, mdl, language_code, pitch, pace

    def _get_pool(self, model: str) -> sarvam_pool.SarvamStreamPool | None:
        if not self.api_key or settings.sarvam_stream_pool_size < 1:
            return None
        pool = self._pools.get(model)
        if pool is None:
            pool = sarvam_pool.SarvamStreamPool(
                api_key=self.api_key,
                model=model,
                min_size=settings.sarvam_stream_pool_size,
                max_size=max(settings.sarvam_stream_pool_size * 2, settings.sarvam_stream_pool_size + 4),
            )
            self._pools[model] = pool
        return pool

    async def warm(self) -> None:
        """Pre-warm the pool for the default model (called at startup)."""
        if not self.api_key or settings.sarvam_stream_pool_size < 1:
            return
        model = PROVIDER_DEFAULTS.get("sarvam", {}).get("model", "bulbul:v3")
        try:
            pool = self._get_pool(model)
            if pool is not None:
                await pool.start()
        except Exception as e:
            logger.warning(f"Sarvam stream pool warm failed: {e}")

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
        """Synthesize ``text`` and return raw 16-bit PCM at 16 kHz.

        Missing fields fall back to ``PROVIDER_DEFAULTS["sarvam"]``. The request
        mirrors clairvoyance's ``_generate_sarvam_audio``: same endpoint,
        ``api-subscription-key`` header, and JSON payload.
        """
        if not self.api_key:
            raise ValueError("SARVAM_API_KEY is required")

        voice, mdl, language_code, pitch, pace = self._resolve(
            voice_id=voice_id, model=model, language=language, params=params
        )

        url = "https://api.sarvam.ai/text-to-speech"
        headers = {
            "api-subscription-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": [text],
            "target_language_code": language_code,
            "speaker": voice,
            "pace": pace,
            "speech_sample_rate": _SARVAM_SAMPLE_RATE,
            "enable_preprocessing": True,
            "model": mdl,
        }
        # bulbul:v3 rejects pitch/loudness ("do not pass these values"); v2
        # accepts them. Preprocessing is always on for v3 regardless.
        if not _is_v3(mdl):
            payload["pitch"] = pitch
            payload["loudness"] = 1.5

        logger.info(f"Synthesizing with Sarvam: {text[:50]}... [model={mdl}]")

        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()

        audios = result.get("audios") or []
        audio_base64 = audios[0] if audios else None
        if not audio_base64:
            raise ProviderError("No audio returned from Sarvam API")

        audio = base64.b64decode(audio_base64)
        return AudioResult(
            audio=audio,
            container="raw",
            encoding="pcm_s16le",
            sample_rate=_SARVAM_SAMPLE_RATE,
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
        """Stream native 16 kHz PCM chunks via the warm Sarvam WS pool.

        Each upstream chunk (at the model's native rate) is resampled to 16 kHz
        and yielded, so a ``/tts/stream`` miss reaches the caller with low TTFB.
        Falls back to one-shot HTTP synth if no warm socket is ready (and nothing
        has streamed yet).

        Raises:
            ProviderError: If the API key is missing, or the socket fails after
                audio has started streaming.
        """
        if not self.api_key:
            raise ProviderError("SARVAM_API_KEY is required")

        voice, mdl, language_code, pitch, pace = self._resolve(
            voice_id=voice_id, model=model, language=language, params=params
        )
        # WS config (matches pipecat's SarvamTTSService): output_audio_codec is
        # "linear16", speech_sample_rate is a STRING, and the model/bitrates are
        # explicit. bulbul:v3 rejects pitch/loudness; v2 accepts them.
        config = {
            "target_language_code": language_code,
            "speaker": voice,
            "speech_sample_rate": str(sarvam_pool.model_sample_rate(mdl)),
            "enable_preprocessing": True,
            "min_buffer_size": 50,
            "max_chunk_length": 150,
            "output_audio_codec": "linear16",
            "output_audio_bitrate": "128k",
            "pace": pace,
            "model": mdl,
        }
        if not _is_v3(mdl):
            config["pitch"] = pitch
            config["loudness"] = 1.5

        logger.info(f"Streaming via Sarvam WS: {text[:50]}... [model={mdl}]")

        pool = self._get_pool(mdl)
        if pool is not None:
            resampler = StreamingResampler(sarvam_pool.model_sample_rate(mdl), 16000)
            streamed_any = False
            try:
                async for chunk in pool.stream(config, text):
                    streamed_any = True
                    out = resampler.push(chunk)
                    if out:
                        yield out
                tail = resampler.flush()
                if tail:
                    yield tail
                return
            except sarvam_pool.SocketUnavailable:
                if streamed_any:
                    raise
                logger.warning(
                    "Sarvam WS unavailable — serving miss via one-shot HTTP synth"
                )
            except Exception as e:
                # WS connected but the utterance failed (config/schema/auth/quota).
                # Only fall back if we haven't streamed partial audio yet.
                if streamed_any:
                    raise
                logger.warning(
                    f"Sarvam WS stream failed ({e}) — "
                    f"serving miss via one-shot HTTP synth"
                )

        # Fallback: one-shot HTTP synth (also used when pooling is disabled).
        result = await self.synth(
            text=text, voice_id=voice_id, model=mdl, language=language_code, params=params,
        )
        yield result.audio
