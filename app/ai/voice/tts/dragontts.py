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

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

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

from app.core.config.static import DRAGONTTS_URL
from app.core.logger import logger

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig

__all__ = [
    "DragonTTSConfig",
    "DragonTTSService",
    "DragonTTSCacheStats",
    "build_dragontts_tts",
    "_generate_dragontts_audio",
]


# Params DragonTTS folds into the cache key and applies per nested provider.
_PARAM_FIELDS = ("speed", "volume", "emotion", "pitch", "style_prompt")


def _word_count(text: str) -> int:
    """Whitespace word count — mirrors DragonTTS's own ``_wc`` so our numbers
    reconcile with its ``/stats/daily`` words_served/words_synthesized."""
    return len(text.split())


@dataclass
class DragonTTSCacheStats:
    """Per-call cache attribution built from DragonTTS ``X-Cache`` headers.

    One instance lives on each :class:`DragonTTSService` (one per pipeline
    generation). ``record`` is called once per aggregated sentence; a turn is
    the set of sentences sharing a pipecat ``context_id``. Any non-HIT status
    (MISS, MISS-STITCH, UNKNOWN) counts as a miss — words the nested provider
    actually synthesized.
    """

    provider: str  # nested provider, e.g. "cartesia"
    model: str  # nested model, e.g. "sonic-3.5"
    hit_requests: int = 0
    miss_requests: int = 0
    hit_words: int = 0
    miss_words: int = 0
    # context_id -> {"hits", "misses", "hit_words", "miss_words"}; insertion
    # order == turn order. Capped so a runaway call can't bloat meta_data.
    turns: Dict[str, Dict[str, int]] = field(default_factory=dict)

    _MAX_TURNS = 200

    def record(self, *, context_id: str, words: int, status: str) -> None:
        is_hit = status == "HIT"
        if is_hit:
            self.hit_requests += 1
            self.hit_words += words
        else:
            self.miss_requests += 1
            self.miss_words += words
        turn = self.turns.get(context_id)
        if turn is None:
            if len(self.turns) >= self._MAX_TURNS:
                return
            turn = {"hits": 0, "misses": 0, "hit_words": 0, "miss_words": 0}
            self.turns[context_id] = turn
        if is_hit:
            turn["hits"] += 1
            turn["hit_words"] += words
        else:
            turn["misses"] += 1
            turn["miss_words"] += words

    @property
    def total_words(self) -> int:
        return self.hit_words + self.miss_words

    @property
    def has_data(self) -> bool:
        return bool(self.hit_requests or self.miss_requests)

    def as_meta(self) -> Dict[str, Any]:
        """Compact dict for ``lead.meta_data['tts_cache']``."""
        total = self.total_words
        return {
            "provider": self.provider,
            "model": self.model,
            "hit_requests": self.hit_requests,
            "miss_requests": self.miss_requests,
            "hit_words": self.hit_words,
            "miss_words": self.miss_words,
            "cache_word_ratio": round(self.hit_words / total, 4) if total else None,
            "turns": [
                {"turn": i + 1, **counters}
                for i, counters in enumerate(self.turns.values())
            ],
        }


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
            response = await client.post(f"{DRAGONTTS_URL}/tts/bytes", json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(
                f"DragonTTS /tts/bytes returned HTTP {e.response.status_code}: "
                f"{e.response.text[:200]}"
            )
            raise
        except httpx.RequestError as e:
            logger.error(f"DragonTTS /tts/bytes request failed: {e}")
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
    """

    url: str
    model_id: str
    voice_id: str
    language: str = ""
    params: dict = field(default_factory=dict)
    aggregate_sentences: bool = True


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
            **kwargs,
        )
        self._url = url.rstrip("/")
        self._model_id = model_id
        self._voice_id = voice_id
        self._language = language or ""
        self._params = dict(params or {})
        self._client: httpx.AsyncClient | None = None
        # Cache attribution from X-Cache response headers. The agent reads this
        # off the service at call end; stats must never break the audio path.
        nested_provider, _, nested_model = model_id.partition(":")
        self.cache_stats = DragonTTSCacheStats(
            provider=nested_provider or model_id, model=nested_model
        )

    def language_to_service_language(self, language: Language) -> str | None:
        """DragonTTS accepts a plain language code — no provider mapping needed."""
        return None

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
        logger.debug(f"{self}: Generating TTS via DragonTTS (len={len(text)})")

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
                response.raise_for_status()
                try:
                    self.cache_stats.record(
                        context_id=context_id,
                        words=_word_count(text),
                        status=response.headers.get("X-Cache", "UNKNOWN"),
                    )
                except Exception as stats_err:  # stats are best-effort only
                    logger.warning(f"DragonTTS cache-stats record failed: {stats_err}")
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
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:200]
            logger.error(f"DragonTTS returned HTTP {e.response.status_code}: {detail}")
            yield ErrorFrame(
                error=f"DragonTTS returned HTTP {e.response.status_code}: {detail}"
            )
        except httpx.RequestError as e:
            logger.error(f"DragonTTS stream request failed: {e}")
            yield ErrorFrame(error=f"DragonTTS stream request failed: {e}")


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
    )
