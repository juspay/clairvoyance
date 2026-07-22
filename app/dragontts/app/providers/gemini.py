"""Gemini TTS provider adapter.

Ports clairvoyance's ``_generate_gemini_audio`` helper into the uniform
``BaseTTSProvider.synth`` contract. On a cache miss DragonTTS delegates raw
synthesis here, then a separate conversion layer maps the native format to the
caller's requested ``output_format`` before caching.

Gemini TTS outputs 24 kHz PCM natively over its gRPC ``streaming_synthesize``
API; we downsample to 16 kHz PCM (the native cache/conversation rate) so a
single entry serves every format on read and live-streams to a 16 kHz caller.

Two paths:
- :meth:`synth` — collect the full 24k clip, downsample once, return (used by
  the one-shot ``/tts/bytes`` path, stitch, and the warmer).
- :meth:`stream_synth` — feed each 24k gRPC chunk through a stateful
  :class:`~app.audio.resample.StreamingResampler` and yield 16k chunks live
  (low TTFB on a ``/tts/stream`` miss). Both converge on the same audio.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import numpy as np
from google.cloud import texttospeech_v1
from google.oauth2 import service_account

from app.audio.resample import StreamingResampler
from app.core.config import PROVIDER_DEFAULTS, settings
from app.core.logging import logger
from app.providers.base import AudioResult, BaseTTSProvider, ProviderError

# Gemini TTS native sample rate — fixed by the API.
_GEMINI_SAMPLE_RATE = 24_000


class GeminiProvider(BaseTTSProvider):
    """Gemini TTS backend.

    Returns audio in Gemini's native raw PCM format (16-bit signed LE),
    downsampled from the API's native 24 kHz to 16 kHz mono — mirroring
    clairvoyance's ``_generate_gemini_audio``.
    """

    name = "gemini"

    def __init__(
        self,
        credentials_json: str | None = None,
        credentials_path: str | None = None,
    ):
        """Initialize the provider.

        Args:
            credentials_json: GCP service-account JSON string. Defaults to
                ``settings.google_credentials_json`` when ``None``.
            credentials_path: Path to a GCP service-account JSON file. Defaults
                to ``settings.google_credentials_path`` when ``None``.
        """
        self.credentials_json = credentials_json or settings.google_credentials_json
        self.credentials_path = credentials_path or settings.google_credentials_path
        # Cached gRPC client + credentials — built once, reused across synths.
        # Building a channel per call is expensive (TLS + gRPC handshake) and a
        # real bottleneck under concurrent misses.
        self._client: texttospeech_v1.TextToSpeechAsyncClient | None = None
        self._creds: service_account.Credentials | None = None

    def _get_client(self) -> texttospeech_v1.TextToSpeechAsyncClient:
        """Lazily build + cache the authenticated async gRPC client."""
        if self._client is None:
            if not self.credentials_json and not self.credentials_path:
                raise ProviderError(
                    "GOOGLE_CREDENTIALS_JSON/PATH is required for Gemini TTS"
                )
            if self.credentials_json:
                json_account_info = json.loads(self.credentials_json)
                creds = service_account.Credentials.from_service_account_info(
                    json_account_info
                )
            else:
                creds = service_account.Credentials.from_service_account_file(
                    self.credentials_path
                )
            self._creds = creds
            self._client = texttospeech_v1.TextToSpeechAsyncClient(credentials=creds)
        return self._client

    def _request_generator(
        self,
        *,
        text: str,
        final_voice_id: str,
        final_model: str,
        final_language: str,
        style_prompt: str | None,
    ):
        """Build the gRPC streaming request generator (config + synthesis input)."""
        voice_params = texttospeech_v1.VoiceSelectionParams(
            language_code=final_language,
            name=final_voice_id,
            model_name=final_model,
        )
        streaming_config = texttospeech_v1.StreamingSynthesizeConfig(
            voice=voice_params,
            streaming_audio_config=texttospeech_v1.StreamingAudioConfig(
                audio_encoding=texttospeech_v1.AudioEncoding.PCM,
                sample_rate_hertz=_GEMINI_SAMPLE_RATE,
            ),
        )

        async def _gen():
            yield texttospeech_v1.StreamingSynthesizeRequest(
                streaming_config=streaming_config
            )
            synthesis_params: dict = {"text": text}
            if style_prompt:
                synthesis_params["prompt"] = style_prompt
            yield texttospeech_v1.StreamingSynthesizeRequest(
                input=texttospeech_v1.StreamingSynthesisInput(**synthesis_params)
            )

        return _gen

    def _resolve(
        self, *, voice_id: str | None, model: str | None, language: str | None, params: dict
    ) -> tuple[str, str, str, str | None]:
        defaults = PROVIDER_DEFAULTS["gemini"]
        return (
            voice_id or defaults.get("voice_id", "Kore"),
            model or defaults.get("model", ""),
            language or defaults.get("language", "en-IN"),
            params.get("style_prompt") if params else None,
        )

    async def synth(
        self,
        *,
        text: str,
        voice_id: str | None,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AudioResult:
        """Synthesize ``text`` and return native-format (16 kHz) raw PCM audio.

        Collects the full 24 kHz gRPC stream then downsamples to 16 kHz —
        matching clairvoyance's reference helper. Used by the one-shot
        ``/tts/bytes`` path, stitch, and the warmer.

        Fields missing from the request fall back to
        ``PROVIDER_DEFAULTS["gemini"]``.

        Args:
            text: The text to synthesize.
            voice_id: Gemini voice name (e.g. "Kore") override, or ``None``/
                empty for the default.
            model: Gemini TTS model override, or ``None``/empty for the
                default.
            language: BCP-47 language code (e.g. "en-IN") override, or
                ``None``/empty for the default.
            params: Reserved for future provider-specific options. ``style_prompt``
                (a natural-language style instruction) is honored if present.

        Returns:
            AudioResult holding raw ``pcm_s16le`` audio at 16 kHz.

        Raises:
            ProviderError: If neither ``credentials_json`` nor ``credentials_path``
                is configured, or Gemini returns empty audio.
        """
        final_voice_id, final_model, final_language, style_prompt = self._resolve(
            voice_id=voice_id, model=model, language=language, params=params
        )
        logger.info(
            f"Synthesizing audio with Gemini: {text[:50]}... "
            f"[voice_id={final_voice_id}, model={final_model}, lang={final_language}]"
        )

        client = self._get_client()
        responses = await client.streaming_synthesize(
            self._request_generator(
                text=text, final_voice_id=final_voice_id, final_model=final_model,
                final_language=final_language, style_prompt=style_prompt,
            )()
        )
        chunks: list[bytes] = []
        try:
            async for response in responses:
                if response.audio_content:
                    chunks.append(response.audio_content)
        finally:
            # Cancel the gRPC stream on early abandon (see stream_synth).
            _cancel = getattr(responses, "cancel", None)
            if _cancel is not None:
                try:
                    _cancel()
                except Exception:
                    pass

        pcm_24k = b"".join(chunks)
        if not pcm_24k:
            raise ProviderError(
                "Gemini TTS returned empty audio — check credentials and voice settings"
            )

        # Ensure whole-frame alignment before resampling (16-bit = 2 bytes/frame).
        if len(pcm_24k) % 2 != 0:
            pcm_24k += b"\x00"

        # Downsample 24 kHz -> 16 kHz via linear interpolation (no scipy needed).
        # 24000 * (2/3) = 16000.
        samples = np.frombuffer(pcm_24k, dtype=np.int16).astype(np.float32)
        out_len = len(samples) * 16_000 // _GEMINI_SAMPLE_RATE
        indices = np.linspace(0, len(samples) - 1, out_len)
        lo = np.floor(indices).astype(np.int32)
        hi = np.minimum(lo + 1, len(samples) - 1)
        frac = (indices - lo).astype(np.float32)
        resampled = samples[lo] + frac * (samples[hi] - samples[lo])
        pcm_16k = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

        return AudioResult(
            audio=pcm_16k,
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
        """Stream native 16 kHz PCM chunks as Gemini synthesizes them.

        Gemini emits 24 kHz PCM over gRPC; each chunk is fed through a stateful
        :class:`StreamingResampler` (24k -> 16k) so the caller receives live 16
        kHz chunks on a cache miss (low TTFB). The accumulated output matches
        :meth:`synth` to within a sub-sample at the tail.
        """
        final_voice_id, final_model, final_language, style_prompt = self._resolve(
            voice_id=voice_id, model=model, language=language, params=params
        )
        logger.info(
            f"Streaming audio via Gemini: {text[:50]}... "
            f"[voice_id={final_voice_id}, model={final_model}, lang={final_language}]"
        )

        client = self._get_client()
        responses = await client.streaming_synthesize(
            self._request_generator(
                text=text, final_voice_id=final_voice_id, final_model=final_model,
                final_language=final_language, style_prompt=style_prompt,
            )()
        )
        resampler = StreamingResampler(_GEMINI_SAMPLE_RATE, 16000)
        try:
            async for response in responses:
                if response.audio_content:
                    chunk = resampler.push(response.audio_content)
                    if chunk:
                        yield chunk
            tail = resampler.flush()
            if tail:
                yield tail
        except Exception as e:
            raise ProviderError(f"gemini stream error: {e}") from e
        finally:
            # Cancel the server-side gRPC stream if the consumer abandoned early
            # (a /tts/stream disconnect throws GeneratorExit, which the except
            # above does NOT catch). Without this the open stream leaks on the
            # process-cached channel and exhausts HTTP/2 concurrency.
            _cancel = getattr(responses, "cancel", None)
            if _cancel is not None:
                try:
                    _cancel()
                except Exception:
                    pass

    async def aclose(self) -> None:
        """Release the cached gRPC channel (built lazily on first use)."""
        if self._client is not None:
            try:
                await self._client.transport.grpc_channel.close()  # type: ignore[union-attr]
            except Exception as e:
                logger.warning(f"Gemini gRPC channel close failed: {e}")
            self._client = None
