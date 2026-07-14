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
TRANSPORT_TYPE_TELEPHONY = "telephony"


def _get_aic_model_path(transport_type: str) -> Path:
    """Select appropriate AIC model based on transport.

    Auto-selects the AIC model's input processing rate based on transport:
    - Daily (web): 16kHz AIC model
    - Telephony (Twilio/Plivo/Exotel): 8kHz AIC model

    Note:
        These rates describe the selected AIC model's expected processing rate,
        not the transport's native audio sample rate. For example, Daily may
        operate at 24kHz elsewhere in the pipeline while still using the
        16kHz AIC model here.

    Args:
        transport_type: The transport type (e.g. TRANSPORT_TYPE_DAILY, TRANSPORT_TYPE_TELEPHONY).

    Returns:
        Path to the selected AIC model file.
    """
    if transport_type == TRANSPORT_TYPE_DAILY:
        return Path(static.AIC_MODEL_PATH_16KHZ)
    return Path(static.AIC_MODEL_PATH)


def _create_audio_input_filter(
    configurations: Optional[ConfigurationModel] = None,
    transport_type: str = TRANSPORT_TYPE_DAILY,
) -> Optional[BaseAudioFilter]:
    """Create audio input filter based on configuration.

    Currently supports:
        - AIC filter (ai-coustics noise enhancement)

    Future filters can be added here with their own enable flags.

    Args:
        configurations: The configuration model containing noise filter settings.
        transport_type: The transport type to determine model selection.

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
        if not static.BREEZE_BUDDY_AIC_LICENSE_KEY:
            logger.warning("AIC filter enabled but license key not configured")
            return None
        model_path = _get_aic_model_path(transport_type)
        try:
            aic_filter = AICFilter(
                license_key=static.BREEZE_BUDDY_AIC_LICENSE_KEY,
                model_path=model_path,
            )
            logger.info(f"AIC filter initialized successfully with model: {model_path}")
            return aic_filter
        except Exception as e:
            logger.warning(
                f"Failed to initialize AIC filter, proceeding without it: {e}"
            )

    return None


def get_transport_params(
    template: Optional[TemplateModel] = None,
    configurations: Optional[ConfigurationModel] = None,
    daily_audio_out_10ms_chunks: int = 10,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    VAD is not configured here — it lives in the LLMUserAggregator
    (via vad_analyzer + UserTurnStrategies) where it feeds turn detection.

    Args:
        template: Optional template model for background sound mixer configuration
        configurations: Optional configuration model for settings (e.g., noise filter)
        daily_audio_out_10ms_chunks: Daily output write size in 10ms chunks.
            The live default comes from the BB_DAILY_AUDIO_OUT_10MS_CHUNKS
            dynamic config, resolved by the async caller and passed in (this
            function is sync); only the daily transport uses it, so telephony
            callers leave the fallback default.

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    # Create separate mixers per transport so each uses the correct sample rate.
    # Daily runs at 24 kHz; all telephony transports run at TELEPHONY_SAMPLE_RATE (8 kHz).
    daily_mixer = create_background_sound_mixer(template, sample_rate=24000)
    telephony_mixer = create_background_sound_mixer(
        template, sample_rate=TELEPHONY_SAMPLE_RATE
    )

    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # Output write size in 10ms chunks (pipecat default 4 = 40ms):
            # bigger chunks give the paced audio writer more event-loop slack
            # against stalls in the bot process, which otherwise underrun
            # Daily's virtual mic and the browser hears crackling
            # (pipecat#331). Rationale + trade-offs on the
            # BB_DAILY_AUDIO_OUT_10MS_CHUNKS dynamic config.
            audio_out_10ms_chunks=daily_audio_out_10ms_chunks,
            audio_out_mixer=daily_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_DAILY
            ),
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=telephony_mixer,
            audio_in_filter=_create_audio_input_filter(
                configurations, TRANSPORT_TYPE_TELEPHONY
            ),
        ),
    }
