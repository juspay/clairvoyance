"""Transport configuration for voice agents."""

from pathlib import Path
from typing import Optional

from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.ai.voice.agents.breeze_buddy.agent.vad import TELEPHONY_SAMPLE_RATE
from app.core.config import dynamic, static
from app.core.logger import logger

# Constants
TRANSPORT_TYPE_DAILY = "daily"


async def _create_audio_input_filter() -> Optional[BaseAudioFilter]:
    """Create audio input filter based on configuration.

    Currently supports:
        - AIC filter (ai-coustics noise enhancement)

    Future filters can be added here with their own enable flags.

    Returns:
        Audio filter instance if enabled and successfully created, None otherwise.
    """
    # AIC Filter
    if (
        await dynamic.ENABLE_BB_AIC_FILTER()
        and static.BREEZE_BUDDY_AICOUSTICS_LICENSE_KEY
    ):
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


async def get_transport_params(
    vad_analyzer: Optional[SileroVADAnalyzer],
    audio_out_mixer: Optional[SoundfileMixer] = None,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    Args:
        vad_analyzer: The VAD analyzer instance to use
        audio_out_mixer: Optional audio mixer for background sounds (only used by telephony transports)

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    audio_in_filter = await _create_audio_input_filter()

    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_filter=audio_in_filter,
            # Note: DailyParams does not support audio_out_mixer
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=audio_in_filter,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=audio_in_filter,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=audio_in_filter,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
            audio_in_filter=audio_in_filter,
        ),
    }
