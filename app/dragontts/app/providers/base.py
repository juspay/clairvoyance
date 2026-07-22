"""Provider adapter contract.

Each adapter ports a clairvoyance ``_generate_*_audio`` helper into a uniform
``synth()`` that returns the provider's *native* output format. A separate
conversion layer (app/audio/format.py) then maps that native format to the
caller's requested ``output_format`` before caching.

``stream_synth()`` is the streaming variant: it yields native-format audio
chunks as the provider synthesizes them (low TTFB on a cache miss). Providers
without a native streaming API inherit the default, which synthesizes the full
clip and yields it as a single chunk — correct, just without the streaming
latency benefit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass


@dataclass
class AudioResult:
    """Synthesized audio in the provider's native output format."""

    audio: bytes
    container: str  # e.g. "raw"
    encoding: str  # e.g. "pcm_s16le", "ulaw"
    sample_rate: int  # e.g. 16000, 8000


class ProviderError(Exception):
    """A provider-level synthesis error (upstream returned an error / failed).

    Maps to HTTP 502 in the API layer (vs. network-unreachable -> 503).
    """


class BaseTTSProvider(ABC):
    """A single TTS backend.

    Subclasses set ``name`` and implement :meth:`synth`. Fields missing from a
    request fall back to the provider defaults in app.core.config.
    """

    name: str

    # Native output format — used to decide whether a streaming request can be
    # served chunk-by-chunk (matching format) or must fall back to one-shot.
    native_encoding: str = "pcm_s16le"
    native_sample_rate: int = 16000

    @abstractmethod
    async def synth(
        self,
        *,
        text: str,
        voice_id: str,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AudioResult:
        """Synthesize ``text`` and return the native-format audio."""
        raise NotImplementedError

    async def stream_synth(
        self,
        *,
        text: str,
        voice_id: str,
        model: str | None,
        language: str | None,
        params: dict,
    ) -> AsyncGenerator[bytes, None]:
        """Yield native-format audio chunks as they are synthesized.

        The default implementation falls back to one-shot :meth:`synth` and
        yields the whole clip as a single chunk. Providers with a streaming API
        override this to emit incremental chunks for low TTFB on cache misses.
        """
        result = await self.synth(
            text=text, voice_id=voice_id, model=model, language=language, params=params
        )
        yield result.audio

    async def warm(self) -> None:
        """Pre-warm provider resources (e.g. a connection pool). Default no-op."""
        return None

    async def aclose(self) -> None:
        """Release provider resources (HTTP pools etc). Default no-op."""
        return None
