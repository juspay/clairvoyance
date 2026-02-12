"""Transport configuration for voice agents."""

from typing import Optional

from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.ai.voice.agents.breeze_buddy.agent.vad import TELEPHONY_SAMPLE_RATE

# Constants
TRANSPORT_TYPE_DAILY = "daily"


def get_transport_params(
    audio_out_mixer: Optional[SoundfileMixer] = None,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    VAD is handled by the user aggregator, not the transport.

    Args:
        audio_out_mixer: Optional audio mixer for background sounds (only used by telephony transports)

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_sample_rate=TELEPHONY_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
    }
