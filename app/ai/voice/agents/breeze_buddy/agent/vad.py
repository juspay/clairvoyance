"""VAD (Voice Activity Detection) configuration for voice agents."""

from typing import Optional

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from app.ai.voice.agents.breeze_buddy.agent.constants import (
    DAILY_SAMPLE_RATE,
    TELEPHONY_SAMPLE_RATE,
    get_sample_rate_for_provider,
)
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.template.vad import build_default_vad_params
from app.core.config.dynamic import (
    BB_DAILY_VAD_CONFIDENCE,
    BB_DAILY_VAD_MIN_VOLUME,
    BB_DAILY_VAD_START_SECS,
    BB_DAILY_VAD_STOP_SECS,
)
from app.core.logger import logger


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
    provider: Optional[str] = None,
    bandwidth: str = "low",
) -> tuple[SileroVADAnalyzer, Optional[VADParams]]:
    """Create VAD analyzer with appropriate parameters.

    Args:
        is_daily_mode: Whether this is Daily mode
        template: Template model for telephony mode VAD params
        provider: Provider name (e.g., 'exotel', 'plivo', 'twilio') for provider-specific sample rates
        bandwidth: Audio bandwidth setting ("low" = 8kHz, "high" = 16kHz). Default is "low".

    Returns:
        Tuple of (SileroVADAnalyzer, default_vad_params for telephony or None for Daily)
    """
    if is_daily_mode:
        params = await create_daily_vad_params()
        return SileroVADAnalyzer(sample_rate=DAILY_SAMPLE_RATE, params=params), None

    # Determine sample rate based on provider and bandwidth configuration
    if provider:
        provider_lower = provider.lower()
        sample_rate = get_sample_rate_for_provider(provider_lower, bandwidth)
        logger.info(
            f"VAD sample rate for provider '{provider}' with bandwidth '{bandwidth}': {sample_rate} Hz"
        )
    else:
        sample_rate = TELEPHONY_SAMPLE_RATE
        logger.debug(
            f"No provider specified for VAD, using default telephony sample rate: {TELEPHONY_SAMPLE_RATE} Hz"
        )

    default_vad_params = build_default_vad_params(template)
    return (
        SileroVADAnalyzer(sample_rate=sample_rate, params=default_vad_params),
        default_vad_params,
    )
