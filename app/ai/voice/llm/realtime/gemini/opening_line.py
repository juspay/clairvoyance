"""Opening-line audio generation for Gemini Live (speech-to-speech) calls.

For realtime templates the LLM's first spoken line dominates perceived call-
connect latency: the customer waits through silence while the pipeline builds,
the Live session connects, and turn-1 audio streams. Gemini Live greetings
are STATIC (variable placeholders are rejected at template save), so the
opening line is fully determined by the template alone — it is generated ONCE
per template (at template save, or lazily at the first call after a cache
miss) and stored in the shared persistent static-template greeting key
(``greeting:template:{id}``, raw base64 mulaw — the same key the TTS path
uses; freshness comes from PUT/DELETE invalidation, not TTL). At connect the
cached audio plays immediately (pre-pipeline) and the fresh Live session
warms up hidden behind it.

The throwaway session uses the SAME model / voice / language the call's
realtime config resolves to (mirroring the factory), so the pre-played audio
is indistinguishable from the live session's own speech.

Everything here fails open: any error or timeout returns None and the call
falls back to LLM-speaks-first, exactly as before this module existed.
"""

import asyncio
import audioop
import time
from typing import Optional

from google import genai
from google.genai import types as gt

from app.ai.voice.llm.realtime.gemini.realtime import (
    DEFAULT_GEMINI_REALTIME_MODEL,
    DEFAULT_GEMINI_REALTIME_VOICE,
)
from app.ai.voice.llm.types import RealtimeConfig
from app.core.config import static
from app.core.logger import logger

__all__ = ["generate_opening_line_mulaw"]

# Bound the whole exercise. Measured: connect ~0.6-1s, then the FULL spoken
# opening line takes ~3-7s more (the model generates the complete turn, not
# just first-audio) — 4s was observed killing the session mid-generation
# (2026-08-18 live run). The dispatch worker AWAITS this before dialing
# (blocking only that lead's dial — never the event loop, other workers, or
# any handler), so the bound is the worst-case dial delay, not a ring-window
# fit. 30s makes a missed greeting virtually impossible while still
# releasing the lead to fail-open (LLM-speaks-first) on a stuck session.
DEFAULT_GENERATION_TIMEOUT_SECONDS = 30.0

# Native-audio Live models emit s16le mono PCM; the inline mime carries the
# rate ("audio/pcm;rate=24000"). Telephony playback needs mulaw at 8kHz.
_DEFAULT_LIVE_PCM_RATE = 24000
_SAMPLE_WIDTH_BYTES = 2  # 16-bit linear PCM
TELEPHONY_SAMPLE_RATE = 8000

_SAY_EXACTLY_PROMPT = (
    "Say the following line out loud exactly as written, in the same language, "
    "in a warm, natural phone-call voice. Say nothing before or after it.\n\n"
)


def _rate_from_mime(mime: Optional[str]) -> int:
    if mime and "rate=" in mime:
        try:
            return int(mime.split("rate=")[1].split(";")[0].strip())
        except ValueError:
            pass
    return _DEFAULT_LIVE_PCM_RATE


async def generate_opening_line_mulaw(
    text: str,
    realtime: RealtimeConfig,
    timeout_seconds: float = DEFAULT_GENERATION_TIMEOUT_SECONDS,
) -> Optional[bytes]:
    """Speak ``text`` with the call's Live model/voice/language.

    Returns mulaw 8kHz mono audio ready for the telephony greeting cache, or
    None on any failure. Never raises — callers fail open.
    """
    if not text or not text.strip():
        return None
    if not static.GEMINI_API_KEY:
        logger.warning("opening-line: GEMINI_API_KEY unset; skipping generation")
        return None
    started = time.perf_counter()
    stats: dict = {"started_at": started}
    try:
        audio = await asyncio.wait_for(
            _generate(text, realtime, stats), timeout=timeout_seconds
        )
        if audio is not None:
            logger.info(
                f"opening-line: generated {len(audio)} bytes mulaw in "
                f"{time.perf_counter() - started:.2f}s "
                f"(first audio after {stats.get('first_audio_after_s', '?')}s)"
            )
        else:
            logger.warning(
                f"opening-line: session ended with no audio after "
                f"{time.perf_counter() - started:.2f}s"
            )
        return audio
    except Exception as e:  # noqa: BLE001 - fail open to LLM-speaks-first
        # chunks>0 at timeout = the model was mid-generation and the bound
        # cut it; chunks=0 = the model never started (or connect stalled).
        logger.opt(exception=e).warning(
            f"opening-line: generation failed after "
            f"{time.perf_counter() - started:.2f}s: {type(e).__name__} "
            f"({stats.get('chunks', 0)} audio chunks received)"
        )
        return None


async def _generate(
    text: str, realtime: RealtimeConfig, stats: dict
) -> Optional[bytes]:
    # Same resolution the realtime factory applies, so the pre-played line is
    # spoken by the exact voice the live session will use.
    model = realtime.model or DEFAULT_GEMINI_REALTIME_MODEL
    voice = realtime.voice or DEFAULT_GEMINI_REALTIME_VOICE
    speech_config = gt.SpeechConfig(
        voice_config=gt.VoiceConfig(
            prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=voice)
        )
    )
    if realtime.language:
        speech_config.language_code = realtime.language

    client = genai.Client(api_key=static.GEMINI_API_KEY)
    config = gt.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=speech_config,
    )

    pcm_chunks: list[bytes] = []
    pcm_rate = _DEFAULT_LIVE_PCM_RATE
    async with client.aio.live.connect(model=model, config=config) as session:
        await session.send_client_content(
            turns=gt.Content(
                role="user", parts=[gt.Part(text=_SAY_EXACTLY_PROMPT + text)]
            ),
            turn_complete=True,
        )
        # Gemini 3.x Live treats client content as history seeding and won't
        # run inference without a realtime input — same nudge pipecat's own
        # send path applies (gemini_live/llm.py _create_single_response).
        # 2.5 models infer from the content alone and skip this.
        if "gemini-3" in model:
            await session.send_realtime_input(text=" ")
        async for message in session.receive():
            server_content = getattr(message, "server_content", None)
            if server_content is None:
                continue
            turn = getattr(server_content, "model_turn", None)
            for part in getattr(turn, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    if not pcm_chunks:
                        pcm_rate = _rate_from_mime(getattr(inline, "mime_type", None))
                        stats["first_audio_at"] = time.perf_counter()
                    pcm_chunks.append(bytes(inline.data))
                    stats["chunks"] = stats.get("chunks", 0) + 1
            if getattr(server_content, "turn_complete", False):
                break

    if not pcm_chunks:
        return None
    if "first_audio_at" in stats:
        stats["first_audio_after_s"] = round(
            stats["first_audio_at"] - stats["started_at"], 2
        )
    pcm = b"".join(pcm_chunks)
    pcm_8k, _state = audioop.ratecv(
        pcm, _SAMPLE_WIDTH_BYTES, 1, pcm_rate, TELEPHONY_SAMPLE_RATE, None
    )
    return audioop.lin2ulaw(pcm_8k, _SAMPLE_WIDTH_BYTES)
