"""Transport configuration for voice agents."""

from typing import Optional

from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.ai.voice.agents.breeze_buddy.agent.constants import (
    EXOTEL_SAMPLE_RATE,
    PLIVO_SAMPLE_RATE,
    TELNYX_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
)

# Constants
TRANSPORT_TYPE_DAILY = "daily"


def get_transport_params(
    vad_analyzer: Optional[SileroVADAnalyzer],
    audio_out_mixer: Optional[SoundfileMixer] = None,
) -> dict:
    """Get transport parameters dictionary for all transport types.

    Provider-specific sample rates for optimal audio quality:
    - Exotel: 16 kHz (wideband, supports up to 24 kHz)
    - Plivo: 16 kHz (wideband, maximum supported)
    - Twilio: 8 kHz (narrowband, network limitation)
    - Telnyx: 8 kHz (narrowband, capabilities unknown)
    - Daily: 16 kHz (wideband WebRTC)

    See docs/AUDIO_SAMPLING_RATE_ANALYSIS.md for detailed analysis.

    Args:
        vad_analyzer: The VAD analyzer instance to use
        audio_out_mixer: Optional audio mixer for background sounds (only used by telephony transports)

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    return {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            # Note: DailyParams does not support audio_out_mixer
            # Sample rate handled internally by Daily (16 kHz WebRTC)
        ),
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,  # 8 kHz (network limitation)
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=EXOTEL_SAMPLE_RATE,  # 16 kHz wideband (2x quality improvement)
            audio_out_sample_rate=EXOTEL_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=TELNYX_SAMPLE_RATE,  # 8 kHz (capabilities unknown)
            audio_out_sample_rate=TELNYX_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=PLIVO_SAMPLE_RATE,  # 16 kHz wideband (2x quality improvement)
            audio_out_sample_rate=PLIVO_SAMPLE_RATE,
            audio_out_mixer=audio_out_mixer,
        ),
    }
