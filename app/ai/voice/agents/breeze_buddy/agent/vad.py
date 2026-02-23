"""VAD (Voice Activity Detection) configuration for voice agents."""

from typing import Optional

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.vad import build_default_vad_params
from app.core.config.dynamic import (
    BB_DAILY_VAD_CONFIDENCE,
    BB_DAILY_VAD_MIN_VOLUME,
    BB_DAILY_VAD_START_SECS,
    BB_DAILY_VAD_STOP_SECS,
    BREEZE_BUDDY_ENABLE_VAD,
)
from app.core.logger import logger

# Constants
TELEPHONY_SAMPLE_RATE = 8000
DAILY_SAMPLE_RATE = 16000


async def create_daily_vad_params() -> VADParams:
    """Create VAD parameters for Daily mode from dynamic config."""
    return VADParams(
        confidence=await BB_DAILY_VAD_CONFIDENCE(),
        start_secs=await BB_DAILY_VAD_START_SECS(),
        stop_secs=await BB_DAILY_VAD_STOP_SECS(),
        min_volume=await BB_DAILY_VAD_MIN_VOLUME(),
    )


async def create_vad_analyzer(
    is_daily_mode: bool,
    template: Optional[TemplateModel] = None,
) -> tuple[Optional[SileroVADAnalyzer], Optional[VADParams]]:
    """Create VAD analyzer with appropriate parameters.

    VAD is gated behind BREEZE_BUDDY_ENABLE_VAD (default False).
    When disabled, returns (None, None) and all VAD-related functionality is skipped.

    Args:
        is_daily_mode: Whether this is Daily mode
        template: Template model for telephony mode VAD params

    Returns:
        Tuple of (SileroVADAnalyzer or None, default_vad_params for telephony or None)
    """
    if not await BREEZE_BUDDY_ENABLE_VAD():
        logger.info("VAD disabled (BREEZE_BUDDY_ENABLE_VAD=false)")
        return None, None

    if is_daily_mode:
        params = await create_daily_vad_params()
        return SileroVADAnalyzer(sample_rate=DAILY_SAMPLE_RATE, params=params), None

    default_vad_params = build_default_vad_params(template)
    return (
        SileroVADAnalyzer(sample_rate=TELEPHONY_SAMPLE_RATE, params=default_vad_params),
        default_vad_params,
    )
