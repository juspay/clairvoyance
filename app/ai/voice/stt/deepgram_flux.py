"""Deepgram Flux STT config and builder.

Flux is Deepgram's conversational model: one stream both transcribes and
decides when the caller's turn is over (``EndOfTurn``), instead of Nova's
silence timers (``endpointing`` / ``utterance_end_ms``). It is a separate
pipecat service on a separate endpoint (``/v2/listen``), selected by a
``flux-*`` model name under the ``deepgram`` provider.

Flux has no formatting options: numbers come back as words ("nine eight
seven", "five g"), so anything that validates digits needs its own step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pipecat.frames.frames import InterimTranscriptionFrame
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from app.core.logger import logger

__all__ = [
    "DeepgramFluxConfig",
    "DeepgramFluxSTTServiceWithInterims",
    "build_deepgram_flux_stt",
]


class DeepgramFluxSTTServiceWithInterims(DeepgramFluxSTTService):
    """Flux that also pushes its live ``Update`` transcripts as interim frames.

    Flux sends the running transcript of the caller's turn as ``Update``
    events, but pipecat 1.1.0 only hands them to an ``on_update`` handler and
    never pushes a frame. Every other provider pushes interims, and the
    pipeline's barge-in and ``interruption.min_words`` count words from them.
    Pushing the updates here, in order and before any final, makes Flux
    interrupt exactly like Soniox / Nova / ElevenLabs: a "haan" under
    ``min_words: 10`` does not stop the bot, ten words do.

    Pushed inline (not from the ``on_update`` event handler, which may run
    as a task) so an interim can never arrive after its turn's final.
    """

    async def _handle_update(self, transcript: str):
        await super()._handle_update(transcript)
        if transcript:
            await self.push_frame(
                InterimTranscriptionFrame(
                    transcript, self._user_id, time_now_iso8601(), None
                )
            )


@dataclass
class DeepgramFluxConfig:
    """Configuration for Deepgram Flux.

    ``None`` leaves a parameter to Deepgram's own default (eot_threshold 0.7,
    eot_timeout_ms 5000, eager end-of-turn off).
    """

    api_key: str
    model: str = "flux-general-multi"
    language_hints: list[str] = field(default_factory=list)
    eot_threshold: Optional[float] = None
    eager_eot_threshold: Optional[float] = None
    eot_timeout_ms: Optional[int] = None
    mip_opt_out: Optional[bool] = True


def _hints(codes: list[str]) -> Optional[list[Language]]:
    hints = []
    for code in codes:
        try:
            hints.append(Language(code))
        except ValueError:
            logger.warning("Deepgram Flux: unknown language hint '{}' dropped", code)
    return hints or None


def build_deepgram_flux_stt(
    config: DeepgramFluxConfig,
) -> DeepgramFluxSTTServiceWithInterims:
    """Create a Deepgram Flux STT service.

    ``should_interrupt=False``: with interims flowing, barge-in is the
    pipeline's, as for every other provider — the template's interruption
    mode and ``min_words`` apply. Flux's own StartOfTurn interruption would
    fire on the first sound and bypass both.
    """
    settings = DeepgramFluxSTTService.Settings(
        model=config.model,
        eot_threshold=config.eot_threshold,
        eager_eot_threshold=config.eager_eot_threshold,
        eot_timeout_ms=config.eot_timeout_ms,
        # language_hints are honoured only by flux-general-multi
        language_hints=(
            _hints(config.language_hints) if "multi" in config.model else None
        ),
    )
    logger.info(
        "Using Deepgram Flux STT (model={}, language_hints={}, eot_threshold={}, "
        "eager_eot_threshold={}, eot_timeout_ms={}, mip_opt_out={})",
        config.model,
        config.language_hints or None,
        config.eot_threshold,
        config.eager_eot_threshold,
        config.eot_timeout_ms,
        config.mip_opt_out,
    )
    return DeepgramFluxSTTServiceWithInterims(
        api_key=config.api_key,
        mip_opt_out=config.mip_opt_out,
        should_interrupt=False,
        settings=settings,
    )
