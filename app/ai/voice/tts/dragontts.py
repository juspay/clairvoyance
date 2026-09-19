"""DragonTTS caching-proxy TTS helpers and builder.

DragonTTS is treated as a first-class TTS provider. The template selects
``provider: "dragontts"`` and sets ``model`` to ``"<provider>:<model>"`` (e.g.
``"cartesia:sonic-3.5"``), ``voice_id``, and the tuning params. DragonTTS
applies the relevant params per nested provider and caches the result.

Two entry points share the same DragonTTS cache (keyed by output_format, so
each path caches in its own format):

- :func:`_generate_dragontts_audio` — one-shot synthesis (returns μ-law 8 kHz
  mono for Twilio). POSTs to DragonTTS ``/tts/bytes``. Used by the
  non-streaming ``generate_audio`` path.
- :class:`DragonTTSService` — a pipecat ``TTSService`` for the **live streaming
  conversation**. Per aggregated sentence it POSTs to DragonTTS ``/tts/stream``
  (raw ``pcm_s16le`` @ 16 kHz) and streams the chunked response, yielding one
  ``TTSAudioRawFrame`` per chunk. DragonTTS serves cached phrases instantly and,
  on a miss, streams from the nested provider over a warm socket pool while
  teeing the clip to the cache — so scripted phrases become permanent hits and
  misses still reach the caller with low TTFB. The base class emits the
  start/stop frames; pipecat streams the audio downstream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import httpx
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
)
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TextAggregationMode, TTSService
from pipecat.transcriptions.language import Language

from app.core.config.dynamic import DRAGONTTS_URL
from app.core.logger import logger

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig

__all__ = [
    "DragonTTSConfig",
    "DragonTTSService",
    "build_dragontts_tts",
    "_generate_dragontts_audio",
]


# Params DragonTTS folds into the cache key and applies per nested provider.
# enable_ssml_parsing MUST be here: without it, the cached/dragontts path drops
# the flag and ElevenLabs parses <break/> as plain text (SSML only "worked" on the
# direct ElevenLabs path, which forwards it separately). It selects a distinct
# warm SSML socket + cache key inside DragonTTS.
_PARAM_FIELDS = (
    "speed",
    "volume",
    "emotion",
    "pitch",
    "style_prompt",
    "enable_ssml_parsing",
    "similarity_boost",
    "stability",
)


def _collect_params(resolved: "TTSConfig") -> dict:
    """Pull the non-None tuning params off a resolved TTSConfig."""
    return {
        key: getattr(resolved, key)
        for key in _PARAM_FIELDS
        if getattr(resolved, key, None) is not None
    }


async def _generate_dragontts_audio(*, text: str, resolved: "TTSConfig") -> bytes:
    """Synthesize via DragonTTS. Returns μ-law (8 kHz mono) bytes.

    ``resolved.model`` carries the nested provider as ``"<provider>:<model>"``.
    All tuning params are forwarded; DragonTTS applies the ones relevant to the
    nested provider and folds them into the cache key.
    """
    body = {
        "model_id": resolved.model,
        "transcript": text,
        "voice": {"id": resolved.voice_id},
        "language": resolved.language or "",
        "output_format": {"container": "raw", "encoding": "mulaw", "sample_rate": 8000},
        "params": _collect_params(resolved),
    }

    logger.info(
        f"Routing TTS via DragonTTS: model_id={resolved.model}, voice_id={resolved.voice_id}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{await DRAGONTTS_URL()}/tts/bytes", json=body
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            # DragonTTS relays the REAL cause in its JSON body, e.g.
            # {"detail": "upstream gemini returned an error: ..."} — surface
            # that as the headline instead of blaming the relay. The greeting
            # wrapper re-logs whatever we raise, so re-raise the parsed detail
            # (chained) rather than the bare HTTPStatusError whose message
            # says only "Server error '502' for url ...".
            try:
                detail = json.loads(e.response.text).get(
                    "detail", e.response.text[:200]
                )
            except Exception:
                detail = e.response.text[:200]
            logger.error(
                f"TTS synth failed (HTTP {e.response.status_code} via DragonTTS "
                f"relay): {str(detail)[:300]}"
            )
            raise RuntimeError(
                f"TTS synth failed (HTTP {e.response.status_code} via DragonTTS "
                f"relay): {str(detail)[:300]}"
            ) from e
        except httpx.RequestError as e:
            # str() is EMPTY for ReadTimeout/ReadError — log the type + repr so
            # the failure mode is identifiable without guessing.
            logger.error(
                f"DragonTTS /tts/bytes request failed: {type(e).__name__}: {e!r}"
            )
            raise
        logger.info(f"DragonTTS /tts/bytes ok: {len(response.content)} bytes")
        return response.content


@dataclass
class DragonTTSConfig:
    """Configuration for the DragonTTS streaming service.

    Attributes:
        url: DragonTTS base URL (env-derived default).
        model_id: Nested provider as ``"<provider>:<model>"`` (e.g.
            ``"cartesia:sonic-3.5"``).
        voice_id: Voice ID to synthesize with.
        language: Language code forwarded to DragonTTS (empty to use the
            nested provider default).
        params: Tuning params (speed/volume/emotion/pitch/style_prompt) folded
            into the cache key and applied per nested provider.
        aggregate_sentences: Whether to buffer LLM output to sentence
            boundaries before synthesizing (lower provider call count).
        text_filters: Pipecat text filters applied before synthesis — the
            DragonTTS path gets the same emoji stripping the direct providers
            get (alert-catalog B3.1: raw emoji reaching nested Gemini trips
            Vertex content-policy rejections mid-call).
    """

    url: str
    model_id: str
    voice_id: str
    language: str = ""
    params: dict = field(default_factory=dict)
    aggregate_sentences: bool = True
    text_filters: Optional[list] = None


class DragonTTSService(TTSService):
    """Pipecat TTS service that routes the live stream through DragonTTS.

    Each aggregated sentence is POSTed to DragonTTS ``/tts/stream``, which
    streams audio chunks back — a cache HIT streams the cached blob, a MISS
    streams from the nested provider as it synthesizes (low TTFB) while teeing
    the full clip to the cache. Each chunk is yielded as its own audio frame so
    pipecat can play it incrementally. Output is raw ``pcm_s16le`` @ 16 kHz
    mono, matching the other providers' streaming output so the downstream
    transport resamples to μ-law for telephony.
    """

    # DragonTTS returns raw pcm_s16le at 16 kHz for this service — fixed.
    OUTPUT_SAMPLE_RATE = 16000
    # No read timeout: streaming responses may pause between chunks (provider is
    # still synthesizing). Connect/pool/write stay bounded.
    _TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

    def __init__(
        self,
        *,
        url: str,
        model_id: str,
        voice_id: str,
        language: str = "",
        params: Optional[dict] = None,
        aggregate_sentences: bool = True,
        text_filters: Optional[list] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            sample_rate=self.OUTPUT_SAMPLE_RATE,
            push_start_frame=True,
            push_stop_frames=True,
            text_aggregation_mode=(
                TextAggregationMode.SENTENCE
                if aggregate_sentences
                else TextAggregationMode.TOKEN
            ),
            # The base class requires a settings store; language is handled
            # directly (DragonTTS takes a plain code), so leave it unset here.
            settings=TTSSettings(model=model_id, voice=voice_id, language=None),
            text_filters=text_filters,
            **kwargs,
        )
        self._url = url.rstrip("/")
        self._model_id = model_id
        self._voice_id = voice_id
        self._language = language or ""
        self._params = dict(params or {})
        self._client: httpx.AsyncClient | None = None

    def language_to_service_language(self, language: Language) -> str | None:
        """DragonTTS accepts a plain language code — no provider mapping needed."""
        return None

    def can_generate_metrics(self) -> bool:
        """Emit TTFB/processing metrics like every built-in TTS provider.

        Without this override pipecat's base returns False and DragonTTS calls
        record no latency at all — the collector never sees a ttfb_ms for the
        service. The base class runs the TTFB clock itself: started when the
        first aggregated sentence arrives, stopped on the first streamed audio
        chunk (cache hit or miss alike).
        """
        return True

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._client = httpx.AsyncClient(timeout=self._TIMEOUT)

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        await self._close_client()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._close_client()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Stream one aggregated sentence via DragonTTS, yielding a frame per chunk."""
        # Length only — the transcript is caller content (PII); it already
        # lives in DragonTTS server logs, not here.
        logger.debug(f"{self}: Generating TTS via DragonTTS: len={len(text)}")

        # Defensive: build a client if start() wasn't invoked by the pipeline.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._TIMEOUT)

        body = {
            "model_id": self._model_id,
            "transcript": text,
            "voice": {"id": self._voice_id},
            "language": self._language,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.OUTPUT_SAMPLE_RATE,
            },
            "params": self._params,
        }

        try:
            # Stream the response so audio frames flow as chunks arrive
            # (HIT: cached blob; MISS: provider-synthesized chunks). Align
            # chunks to 2-byte (16-bit sample) boundaries so every frame is a
            # whole number of samples; carry any trailing odd byte to the next.
            carry = b""
            async with self._client.stream(
                "POST", f"{self._url}/tts/stream", json=body
            ) as response:
                if response.status_code >= 400:
                    # Read the error body WHILE the stream context is open —
                    # after it closes a streaming response's body is gone, so
                    # raise_for_status()+e.response.text loses the reason.
                    # DragonTTS returns {"detail": "upstream <provider> returned
                    # an error: <reason>"} on a provider failure; surface THAT
                    # so the real cause (e.g. a Gemini error) reaches the call,
                    # not a generic "DragonTTS stream failed".
                    err = await response.aread()
                    try:
                        detail = json.loads(err).get(
                            "detail", err.decode("utf-8", "replace")
                        )
                    except Exception:
                        detail = err.decode("utf-8", "replace")
                    msg = (
                        f"TTS stream failed (HTTP {response.status_code} via "
                        f"DragonTTS relay): {str(detail)[:300]}"
                    )
                    logger.error(msg)
                    yield ErrorFrame(error=msg)
                    return
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    data = carry + chunk
                    usable = len(data) - (len(data) % 2)
                    if usable <= 0:
                        carry = data
                        continue
                    carry = data[usable:]
                    yield TTSAudioRawFrame(
                        audio=data[:usable],
                        sample_rate=self.OUTPUT_SAMPLE_RATE,
                        num_channels=1,
                        context_id=context_id,
                    )
            # A trailing odd byte (carry is always 0 or 1 byte here) can't form a
            # complete 16-bit sample, so drop it rather than emit a corrupt frame.
        except httpx.RequestError as e:
            # str() is EMPTY for ReadTimeout/ReadError; include the type so the
            # truncation family is attributable from the log line alone.
            logger.error(f"DragonTTS stream request failed: {type(e).__name__}: {e!r}")
            yield ErrorFrame(
                error=f"DragonTTS stream request failed: {type(e).__name__}: {e}"
            )


def build_dragontts_tts(config: DragonTTSConfig) -> DragonTTSService:
    """Create a DragonTTS streaming service.

    Args:
        config: DragonTTSConfig instance with TTS parameters.

    Returns:
        Configured DragonTTSService instance.
    """
    return DragonTTSService(
        url=config.url,
        model_id=config.model_id,
        voice_id=config.voice_id,
        language=config.language,
        params=config.params,
        aggregate_sentences=config.aggregate_sentences,
        text_filters=config.text_filters,
    )
