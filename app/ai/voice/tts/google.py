"""Google TTS helpers and builder.

Uses GoogleTTSService from pipecat for real-time pipeline synthesis (Chirp 3
HD voices) and Google Cloud's streaming TTS API directly for pre-call greeting
synthesis.

Google Chirp 3 HD outputs 24 kHz PCM.  The `_generate_google_audio` helper
downsamples to 16 kHz before returning so it is compatible with the shared
`convert_to_mulaw` path (which assumes 16 kHz raw PCM input).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np
from google.cloud import texttospeech_v1
from google.oauth2 import service_account
from pipecat.services.google.tts import GoogleTTSService
from pipecat.transcriptions.language import Language

from app.core.config.static import GOOGLE_CREDENTIALS_JSON
from app.core.logger import logger

__all__ = ["GoogleConfig", "build_google_tts", "_generate_google_audio"]

# Google Chirp 3 HD native streaming sample rate
_GOOGLE_SAMPLE_RATE = 24_000


@dataclass
class GoogleConfig:
    """Configuration for Google TTS."""

    voice_id: str
    language: Language = Language.EN_IN
    credentials: Optional[Any] = None
    text_filters: Optional[Sequence] = None


def build_google_tts(config: GoogleConfig):
    """Create a Google TTS service."""

    text_filters = list(config.text_filters) if config.text_filters else None

    return GoogleTTSService(
        voice_id=config.voice_id,
        settings=GoogleTTSService.Settings(language=config.language),
        credentials=config.credentials,
        text_filters=text_filters,
    )


async def _generate_google_audio(
    text: str,
    voice_id: str | None = None,
    language: str | None = None,
) -> bytes:
    """Synthesize audio via the Google Cloud streaming TTS API (Chirp 3 HD).

    Uses the same streaming_synthesize RPC that GoogleTTSService uses inside the
    pipeline, so the output is identical. Unlike Gemini, the Chirp voice name
    (e.g. "en-IN-Chirp3-HD-Despina") encodes the model, so no model_name is set.

    Args:
        text: Text to synthesize.
        voice_id: Chirp voice name. Defaults to "en-IN-Chirp3-HD-Despina".
        language: BCP-47 language code (e.g. "en-IN"). Defaults to "en-IN" and
            should match the voice's locale prefix.

    Returns:
        Raw PCM bytes (16-bit, mono, **16 kHz**) after downsampling from
        Chirp's native 24 kHz output. Compatible with `convert_to_mulaw`
        when called with `input_format="raw"`.

    Raises:
        ValueError: If GOOGLE_CREDENTIALS_JSON is not set.
    """
    credentials_json = GOOGLE_CREDENTIALS_JSON
    if not credentials_json:
        raise ValueError(
            "GOOGLE_CREDENTIALS_JSON is required for Google TTS pre-synthesis"
        )

    final_voice = voice_id or "en-IN-Chirp3-HD-Despina"
    final_language = language or "en-IN"

    logger.info(
        f"Pre-synthesizing Google audio: {text[:50]}... "
        f"[voice={final_voice}, lang={final_language}]"
    )

    json_account_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(json_account_info)
    client = texttospeech_v1.TextToSpeechAsyncClient(credentials=creds)

    try:
        # Chirp voice name selects the model — no model_name needed.
        voice_params = texttospeech_v1.VoiceSelectionParams(
            language_code=final_language,
            name=final_voice,
        )

        streaming_config = texttospeech_v1.StreamingSynthesizeConfig(
            voice=voice_params,
            streaming_audio_config=texttospeech_v1.StreamingAudioConfig(
                audio_encoding=texttospeech_v1.AudioEncoding.PCM,
                sample_rate_hertz=_GOOGLE_SAMPLE_RATE,
            ),
        )

        async def _request_generator():
            yield texttospeech_v1.StreamingSynthesizeRequest(
                streaming_config=streaming_config
            )
            yield texttospeech_v1.StreamingSynthesizeRequest(
                input=texttospeech_v1.StreamingSynthesisInput(text=text)
            )

        chunks: list[bytes] = []
        streaming_responses = await client.streaming_synthesize(_request_generator())
        async for response in streaming_responses:
            if response.audio_content:
                chunks.append(response.audio_content)

        pcm_24k = b"".join(chunks)

        if not pcm_24k:
            raise RuntimeError(
                "Google TTS returned empty audio — check credentials and voice settings"
            )

        # Ensure whole-frame alignment before resampling (16-bit = 2 bytes/frame)
        if len(pcm_24k) % 2 != 0:
            pcm_24k += b"\x00"

        # Downsample 24 kHz → 16 kHz via linear interpolation (no scipy needed).
        samples = np.frombuffer(pcm_24k, dtype=np.int16).astype(np.float32)
        out_len = len(samples) * 16_000 // _GOOGLE_SAMPLE_RATE
        indices = np.linspace(0, len(samples) - 1, out_len)
        lo = np.floor(indices).astype(np.int32)
        hi = np.minimum(lo + 1, len(samples) - 1)
        frac = (indices - lo).astype(np.float32)
        resampled = samples[lo] + frac * (samples[hi] - samples[lo])
        pcm_16k = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
        return pcm_16k
    finally:
        await client.transport.grpc_channel.close()  # type: ignore[union-attr]
