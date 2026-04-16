"""Transport configuration for voice agents.

Note: VAD (Voice Activity Detection) is configured in the LLMUserAggregator
(via UserTurnStrategies), NOT in the transport. The transport only handles
audio I/O, sample rates, and optional audio filters/mixers.
"""

from pathlib import Path
from typing import Optional

from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    NoiseFilterType,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.template.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.agents.breeze_buddy.utils.audio_mixer import (
    create_background_sound_mixer,
)
from app.core.config import static
from app.core.logger import logger

# Constants
TRANSPORT_TYPE_DAILY = "daily"


def _create_audio_input_filter(
    configurations: Optional[ConfigurationModel] = None,
) -> Optional[BaseAudioFilter]:
    """Create audio input filter based on configuration.

    Currently supports:
        - AIC filter (ai-coustics noise enhancement)

    Future filters can be added here with their own enable flags.

    Args:
        configurations: The configuration model containing noise filter settings.

    Returns:
        Audio filter instance if enabled and successfully created, None otherwise.
    """
    # Check if noise filter is configured and enabled
    if not configurations:
        return None

    noise_filter_config = configurations.noise_filter
    if not noise_filter_config or not noise_filter_config.enable:
        return None

    # Create filter based on type
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
    template: Optional[TemplateModel] = None,
    configurations: Optional[ConfigurationModel] = None,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    VAD is not configured here — it lives in the LLMUserAggregator
    (via vad_analyzer + UserTurnStrategies) where it feeds turn detection.

    Args:
        template: Optional template model for background sound mixer configuration
        configurations: Optional configuration model for settings (e.g., noise filter)

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    # Create separate mixers per transport so each uses the correct sample rate.
    # Daily runs at 24 kHz; all telephony transports run at TELEPHONY_SAMPLE_RATE (8 kHz).
    daily_mixer = create_background_sound_mixer(template, sample_rate=24000)
    telephony_mixer = create_background_sound_mixer(
        template, sample_rate=TELEPHONY_SAMPLE_RATE
    )
    audio_in_filter = _create_audio_input_filter(configurations)

    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_filter=audio_in_filter,
            audio_out_mixer=daily_mixer,
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=audio_in_filter,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=audio_in_filter,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=audio_in_filter,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=audio_in_filter,
        ),
    }
