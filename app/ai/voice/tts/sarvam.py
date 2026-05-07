"""Sarvam TTS helpers and builder."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

import httpx
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language

from app.core.config.dynamic import (
    BB_SARVAM_TTS_ENABLE_PREPROCESSING,
    BB_VOICE_PROVIDER_DEFAULTS,
)
from app.core.config.static import SARVAM_API_KEY
from app.core.logger import logger

__all__ = [
    "SarvamTTSConfig",
    "get_sarvam_language",
    "build_sarvam_tts",
    "_generate_sarvam_audio",
]


@dataclass
class SarvamTTSConfig:
    """Configuration for Sarvam TTS."""

    api_key: str
    voice_id: str
    model: str
    pitch: float
    pace: float
    language_code: Optional[str] = None
    enable_preprocessing: bool = True


def get_sarvam_language(language_code: Optional[str]) -> Language:
    """Convert SARVAM language code to :class:`Language` for TTS.

    Falls back to ``Language.EN_IN`` if an invalid or missing code is provided.
    """

    if language_code:
        try:
            return Language(language_code)
        except ValueError:
            logger.warning(
                "Invalid TTS language code: %s, falling back to EN_IN", language_code
            )

    logger.warning("No SARVAM TTS language code provided, falling back to EN_IN")
    return Language.EN_IN


def build_sarvam_tts(config: SarvamTTSConfig):
    """Create a Sarvam TTS service."""

    language = get_sarvam_language(config.language_code)

    logger.info(
        f"Using Sarvam TTS service with model={config.model}, voice_id={config.voice_id}, "
        f"language={language}, pitch={config.pitch}, pace={config.pace}"
    )

    return SarvamTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model=config.model,
        settings=SarvamTTSService.Settings(
            language=language,
            pitch=config.pitch,
            pace=config.pace,
            enable_preprocessing=config.enable_preprocessing,
        ),
    )


async def _generate_sarvam_audio(
    text: str,
    voice_id: str | None = None,
    model: str | None = None,
    language: str | None = None,
    speed: float | None = None,
    pitch: float | None = None,
) -> bytes:
    """Synthesize audio using Sarvam TTS API.

    Args:
        text: The text to synthesize
        voice_id: Optional voice ID override.
        model: Optional model override.
        language: Optional language code override.
        speed: Optional pace override.
        pitch: Optional pitch override.
    """
    if not SARVAM_API_KEY:
        raise ValueError("SARVAM_API_KEY is required for Sara voice")

    defaults = await BB_VOICE_PROVIDER_DEFAULTS("sarvam")
    model = model or defaults.get("model", "bulbul:v2")
    voice_id = voice_id or defaults.get("voice_id", "manisha")
    language_code = language or defaults.get("language", "en-IN")
    pitch = pitch if pitch is not None else defaults.get("pitch", 0.0)
    pace = speed if speed is not None else defaults.get("speed", 0.9)
    enable_preprocessing = await BB_SARVAM_TTS_ENABLE_PREPROCESSING()

    url = "https://api.sarvam.ai/text-to-speech"
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "inputs": [text],
        "target_language_code": language_code,
        "speaker": voice_id,
        "pitch": pitch,
        "pace": pace,
        "loudness": 1.5,
        "speech_sample_rate": 16000,
        "enable_preprocessing": enable_preprocessing,
        "model": model,
    }

    logger.info(f"Synthesizing greeting with Sarvam: {text[:50]}...")

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        result = response.json()

        audio_base64 = result.get("audios", [None])[0]
        if not audio_base64:
            raise Exception("No audio returned from Sarvam API")

        return base64.b64decode(audio_base64)
