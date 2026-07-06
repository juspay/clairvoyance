"""Gemini TTS helpers and builder.

Uses GeminiTTSService from pipecat for real-time pipeline synthesis and
Google Cloud's streaming TTS API directly for pre-call greeting synthesis.

Gemini TTS outputs 24 kHz PCM.  The `_generate_gemini_audio` helper
downsamples to 16 kHz before returning so it is compatible with the shared
`convert_to_mulaw` path (which assumes 16 kHz raw PCM input).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from google.cloud import texttospeech_v1
from pipecat.services.google.tts import GeminiTTSService
from pipecat.transcriptions.language import Language

from app.core.config.dynamic import GEMINI_TTS_MODEL
from app.core.config.static import GOOGLE_CREDENTIALS_JSON
from app.core.logger import logger
from app.services.gcp.credentials import get_google_auth_input, get_google_credentials

__all__ = ["GeminiConfig", "build_gemini_tts", "_generate_gemini_audio"]

# Gemini TTS native sample rate — fixed by the API
_GEMINI_SAMPLE_RATE = 24_000


@dataclass
class GeminiConfig:
    """Configuration for Gemini TTS.

    voice_id must be one of the 30 Gemini voices (star/moon names).
    See GeminiTTSService.AVAILABLE_VOICES for the full list.

    style_prompt is an optional natural-language style instruction
    (e.g. "Speak in a warm, enthusiastic tone") that Gemini honours
    alongside the synthesised text.
    """

    voice_id: str = "Kore"
    model: str | None = None
    language: Language = Language.EN_IN
    style_prompt: Optional[str] = None
    credentials: Optional[str] = None
    text_filters: Optional[Sequence] = None


async def build_gemini_tts(config: GeminiConfig) -> GeminiTTSService:
    """Create a GeminiTTSService for use in a real-time pipeline.

    The service is configured at 24 kHz (Gemini's native rate).
    When used with a telephony transport (8 kHz) pipecat's transport
    layer handles the resampling.
    """
    text_filters = list(config.text_filters) if config.text_filters else None
    model = config.model or await GEMINI_TTS_MODEL()
    legacy_credentials_json = config.credentials or GOOGLE_CREDENTIALS_JSON
    auth = await asyncio.to_thread(
        get_google_auth_input,
        credentials_json=legacy_credentials_json,
        service_name="Gemini TTS",
    )

    def _build(credentials_arg: str | None) -> GeminiTTSService:
        return GeminiTTSService(
            credentials=credentials_arg,
            settings=GeminiTTSService.Settings(
                model=model,
                voice=config.voice_id,
                language=config.language,
                prompt=config.style_prompt,
            ),
            text_filters=text_filters,
        )

    return _build(auth.value)


async def _generate_gemini_audio(
    text: str,
    voice_id: str | None = None,
    model: str | None = None,
    language: str | None = None,
    style_prompt: str | None = None,
) -> bytes:
    """Synthesize audio via the Gemini TTS streaming API.

    Uses the same Google Cloud streaming_synthesize RPC that GeminiTTSService
    uses inside the pipeline, so the output is identical.

    Args:
        text: Text to synthesize.
        voice_id: Gemini voice name (e.g. "Kore"). Defaults to "Kore".
        model: Gemini TTS model. Defaults to GEMINI_TTS_MODEL (Redis-configurable).
        language: BCP-47 language code (e.g. "en-IN"). Defaults to "en-IN".
        style_prompt: Optional natural-language style instruction.

    Returns:
        Raw PCM bytes (16-bit, mono, **16 kHz**) after downsampling from
        Gemini's native 24 kHz output.  Compatible with `convert_to_mulaw`
        when called with `input_format="raw"`.

    Raises:
        ValueError: If neither ADC nor GOOGLE_CREDENTIALS_JSON is available.
    """
    final_voice = voice_id or "Kore"
    final_model = model or await GEMINI_TTS_MODEL()
    final_language = language or "en-IN"

    logger.info(
        f"Pre-synthesizing Gemini audio: {text[:50]}... "
        f"[voice={final_voice}, model={final_model}, lang={final_language}]"
    )

    # Build authenticated client
    auth = await asyncio.to_thread(
        get_google_credentials,
        credentials_json=GOOGLE_CREDENTIALS_JSON,
        service_name="Gemini TTS pre-synthesis",
    )
    client = texttospeech_v1.TextToSpeechAsyncClient(credentials=auth.credentials)

    try:
        # Voice params — model_name routes to the Gemini TTS model
        voice_params = texttospeech_v1.VoiceSelectionParams(
            language_code=final_language,
            name=final_voice,
            model_name=final_model,
        )

        streaming_config = texttospeech_v1.StreamingSynthesizeConfig(
            voice=voice_params,
            streaming_audio_config=texttospeech_v1.StreamingAudioConfig(
                audio_encoding=texttospeech_v1.AudioEncoding.PCM,
                sample_rate_hertz=_GEMINI_SAMPLE_RATE,
            ),
        )

        async def _request_generator():
            yield texttospeech_v1.StreamingSynthesizeRequest(
                streaming_config=streaming_config
            )
            synthesis_params: dict = {"text": text}
            if style_prompt:
                synthesis_params["prompt"] = style_prompt
            yield texttospeech_v1.StreamingSynthesizeRequest(
                input=texttospeech_v1.StreamingSynthesisInput(**synthesis_params)
            )

        chunks: list[bytes] = []
        streaming_responses = await client.streaming_synthesize(_request_generator())
        async for response in streaming_responses:
            if response.audio_content:
                chunks.append(response.audio_content)

        pcm_24k = b"".join(chunks)

        if not pcm_24k:
            raise RuntimeError(
                "Gemini TTS returned empty audio — check credentials and voice settings"
            )

        # Ensure whole-frame alignment before resampling (16-bit = 2 bytes/frame)
        if len(pcm_24k) % 2 != 0:
            pcm_24k += b"\x00"

        # Downsample 24 kHz → 16 kHz via linear interpolation (no scipy needed).
        # 24000 × (2/3) = 16000 — pick every 2 output samples from every 3 input samples.
        samples = np.frombuffer(pcm_24k, dtype=np.int16).astype(np.float32)
        out_len = len(samples) * 16_000 // _GEMINI_SAMPLE_RATE
        indices = np.linspace(0, len(samples) - 1, out_len)
        lo = np.floor(indices).astype(np.int32)
        hi = np.minimum(lo + 1, len(samples) - 1)
        frac = (indices - lo).astype(np.float32)
        resampled = samples[lo] + frac * (samples[hi] - samples[lo])
        pcm_16k = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
        return pcm_16k
    finally:
        await client.transport.grpc_channel.close()  # type: ignore[union-attr]
