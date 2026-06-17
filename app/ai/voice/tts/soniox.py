"""Soniox TTS helpers and builder.

Wraps pipecat's :class:`SonioxTTSService` (WebSocket streaming TTS) to fit the
shared builder pattern used by other providers in this package, and provides a
one-shot WebSocket synth helper for greeting prep — pipecat's Soniox client
is streaming-only, so the one-shot path is implemented directly against the
same WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Optional, Sequence

from pipecat.services.soniox.tts import (
    SonioxTTSService,
    language_to_soniox_tts_language,
)
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language
from websockets.asyncio.client import connect as websocket_connect

from app.core.config.static import SONIOX_API_KEY
from app.core.logger import logger

__all__ = [
    "SonioxTTSConfig",
    "build_soniox_tts",
    "_generate_soniox_audio",
]


SONIOX_TTS_WS_URL = "wss://tts-rt.soniox.com/tts-websocket"

# Outer bound for the one-shot greeting synth, matching the 30s timeout the
# HTTP one-shot helpers (sarvam, elevenlabs) use. websockets' own
# open_timeout/ping defaults cover the handshake and dead peers; this guards
# the case of a connected server that never sends `terminated`.
SONIOX_TTS_SYNTH_TIMEOUT_SECS = 30.0


@dataclass
class SonioxTTSConfig:
    """Configuration for Soniox TTS."""

    api_key: str
    voice: str
    model: str
    language: Language = Language.EN
    sample_rate: int = 16000
    audio_format: str = "pcm_s16le"
    aggregate_sentences: bool = True
    text_filters: Sequence = field(default_factory=list)


def build_soniox_tts(config: SonioxTTSConfig) -> SonioxTTSService:
    """Create a Soniox TTS service.

    Pipecat handles Language enum -> Soniox language code conversion at
    init time via ``language_to_service_language``, so the enum is forwarded
    untouched.
    """

    logger.info(
        f"Using SonioxTTSService with model={config.model}, voice={config.voice}, "
        f"language={config.language}, sample_rate={config.sample_rate}, "
        f"audio_format={config.audio_format}"
    )

    return SonioxTTSService(
        api_key=config.api_key,
        sample_rate=config.sample_rate,
        audio_format=config.audio_format,
        settings=SonioxTTSService.Settings(
            voice=config.voice,
            model=config.model,
            language=config.language,
        ),
        text_aggregation_mode=(
            TextAggregationMode.SENTENCE
            if config.aggregate_sentences
            else TextAggregationMode.TOKEN
        ),
        text_filters=list(config.text_filters) if config.text_filters else None,
    )


async def _generate_soniox_audio(
    text: str,
    voice: Optional[str] = None,
    model: Optional[str] = None,
    language: Optional[str] = None,
    sample_rate: int = 16000,
) -> bytes:
    """One-shot synth via Soniox WebSocket for greeting prep.

    Opens a single WebSocket, sends config + text + ``text_end:true``, collects
    base64-encoded audio chunks until ``terminated``, and returns the
    concatenated PCM bytes.

    Returns 16-bit little-endian PCM mono at the requested ``sample_rate``,
    matching ``convert_to_mulaw`` expectations for downstream telephony use.
    """
    if not SONIOX_API_KEY:
        raise ValueError("SONIOX_API_KEY is required for Soniox TTS")

    voice = voice or "Priya"
    model = model or "tts-rt-v1"

    if language:
        try:
            lang_enum = Language(language)
        except ValueError:
            logger.warning(
                f"Invalid Soniox language code '{language}', falling back to EN"
            )
            lang_enum = Language.EN
    else:
        lang_enum = Language.EN

    soniox_lang = language_to_soniox_tts_language(lang_enum) or "en"

    config_msg = {
        "api_key": SONIOX_API_KEY,
        "stream_id": "greeting",
        "model": model,
        "voice": voice,
        "language": soniox_lang,
        "audio_format": "pcm_s16le",
        "sample_rate": sample_rate,
    }
    text_msg = {"text": text, "text_end": True, "stream_id": "greeting"}

    logger.info(
        f"Synthesizing greeting with Soniox (pcm_s16le {sample_rate}): {text[:50]}..."
    )

    audio_chunks: list[bytes] = []
    try:
        async with asyncio.timeout(SONIOX_TTS_SYNTH_TIMEOUT_SECS):
            async with websocket_connect(SONIOX_TTS_WS_URL) as ws:
                await ws.send(json.dumps(config_msg))
                await ws.send(json.dumps(text_msg))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    error_code = msg.get("error_code")
                    if error_code is not None:
                        error_message = msg.get("error_message", "")
                        raise Exception(
                            f"Soniox TTS error {error_code}: {error_message}"
                        )

                    audio_b64 = msg.get("audio")
                    if audio_b64:
                        audio_chunks.append(base64.b64decode(audio_b64))

                    if msg.get("terminated"):
                        break
    except asyncio.TimeoutError:
        raise Exception(
            f"Soniox TTS synth timed out after {SONIOX_TTS_SYNTH_TIMEOUT_SECS}s"
        )

    if not audio_chunks:
        raise Exception("No audio returned from Soniox TTS")

    return b"".join(audio_chunks)
