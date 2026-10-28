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

from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = [
    "DeepgramFluxConfig",
    "build_deepgram_flux_stt",
]


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
) -> DeepgramFluxSTTService:
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
    return DeepgramFluxSTTService(
        api_key=config.api_key,
        mip_opt_out=config.mip_opt_out,
        should_interrupt=False,
        settings=settings,
    )
