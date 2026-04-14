"""Transport configuration for voice agents.

Note: VAD (Voice Activity Detection) is configured in the LLMUserAggregator
(via UserTurnStrategies), NOT in the transport. The transport only handles
audio I/O, sample rates, and optional audio filters/mixers.
"""

from pathlib import Path
from typing import Optional

from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.ai.voice.agents.breeze_buddy.agent.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    NoiseFilterType,
)
from app.core.config import static
from app.core.logger import logger

# Constants
TRANSPORT_TYPE_DAILY = "daily"


def _create_audio_input_filter(
    configurations: Optional[ConfigurationModel] = None,
    transport_type: str = "telephony",
) -> Optional[BaseAudioFilter]:
    """Create audio input filter based on configuration and transport type.

    Supports:
        - AIC filter (ai-coustics noise enhancement) for telephony (8kHz)
        - RNNoise filter for Daily/web (16kHz+, resamples internally to 48kHz)

    Args:
        configurations: The configuration model containing noise filter settings.
        transport_type: "telephony" or "daily". Determines which filter to use.

    Returns:
        Audio filter instance if enabled and successfully created, None otherwise.
    """
    # Check if noise filter is configured and enabled
    if not configurations:
        return None

    noise_filter_config = configurations.noise_filter
    if not noise_filter_config or not noise_filter_config.enable:
        return None

    # Use RNNoise for Daily mode (supports any sample rate via internal resampling)
    if transport_type == TRANSPORT_TYPE_DAILY:
        logger.info("Using RNNoise filter for Daily mode (16kHz support)")
        return RNNoiseFilter()

    # Use AIC for telephony (optimized for 8kHz)
    if noise_filter_config.type == NoiseFilterType.AIC:
        if not static.BREEZE_BUDDY_AICOUSTICS_LICENSE_KEY:
            logger.warning("AIC filter enabled but license key not configured")
            return None
        try:
            return AICFilter(
                license_key=static.BREEZE_BUDDY_AICOUSTICS_LICENSE_KEY,
                model_path=Path(static.AIC_MODEL_PATH),
            )
        except Exception as e:
            logger.warning(
                f"Failed to initialize AIC filter, proceeding without it: {e}"
            )

    return None


def get_transport_params(
    audio_out_mixer: Optional[SoundfileMixer] = None,
    configurations: Optional[ConfigurationModel] = None,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    VAD is not configured here — it lives in the LLMUserAggregator
    (via vad_analyzer + UserTurnStrategies) where it feeds turn detection.

    Args:
        audio_out_mixer: Optional audio mixer for background sounds (only used by telephony transports)
        configurations: Optional configuration model for settings (e.g., noise filter)

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    # Create transport-specific filters (AIC for telephony 8kHz, RNNoise for Daily 16kHz+)
    daily_filter = _create_audio_input_filter(configurations, TRANSPORT_TYPE_DAILY)
    telephony_filter = _create_audio_input_filter(configurations, "telephony")

    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_filter=daily_filter,
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=telephony_filter,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=telephony_filter,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=telephony_filter,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=telephony_filter,
        ),
    }
