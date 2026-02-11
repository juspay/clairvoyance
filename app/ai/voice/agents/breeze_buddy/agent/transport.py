"""Transport configuration for voice agents."""

from typing import Optional

from fastapi import WebSocket
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.transports.daily.transport import DailyParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.ai.voice.agents.breeze_buddy.agent.constants import (
    SAMPLE_RATE_LOW,
    get_sample_rate_for_provider,
)
from app.ai.voice.agents.breeze_buddy.serializers.plivo_l16 import (
    PlivoL16FrameSerializer,
)
from app.core.config.static import PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN
from app.core.logger import logger

# Constants
TRANSPORT_TYPE_DAILY = "daily"


def get_transport_params(
    vad_analyzer: Optional[SileroVADAnalyzer],
    audio_out_mixer: Optional[SoundfileMixer] = None,
    bandwidth: str = "low",
) -> dict:
    """Get transport parameters dictionary for all transport types.

    Audio sample rates are determined by the bandwidth configuration:
    - "low": 8 kHz (narrowband) - Compatible with all providers
    - "high": 16 kHz (wideband) - Better quality, requires provider support

    Note: Some providers (e.g., Twilio) are limited to 8 kHz regardless of bandwidth.
    See docs/AUDIO_SAMPLING_RATE_ANALYSIS.md for detailed analysis.

    Args:
        vad_analyzer: The VAD analyzer instance to use
        audio_out_mixer: Optional audio mixer for background sounds (only used by telephony transports)
        bandwidth: Audio bandwidth setting ("low" or "high"). Default is "low".

    Returns:
        Dictionary mapping transport types to parameter factory functions
    """
    # Get sample rates based on bandwidth and provider capabilities
    twilio_rate = get_sample_rate_for_provider("twilio", bandwidth)
    exotel_rate = get_sample_rate_for_provider("exotel", bandwidth)
    telnyx_rate = get_sample_rate_for_provider("telnyx", bandwidth)
    plivo_rate = get_sample_rate_for_provider("plivo", bandwidth)

    logger.info(
        f"Transport params with bandwidth={bandwidth}: "
        f"twilio={twilio_rate}Hz, exotel={exotel_rate}Hz, "
        f"telnyx={telnyx_rate}Hz, plivo={plivo_rate}Hz"
    )

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
            audio_in_sample_rate=twilio_rate,
            audio_out_sample_rate=twilio_rate,
            audio_out_mixer=audio_out_mixer,
        ),
        "exotel": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=exotel_rate,
            audio_out_sample_rate=exotel_rate,
            audio_out_mixer=audio_out_mixer,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=telnyx_rate,
            audio_out_sample_rate=telnyx_rate,
            audio_out_mixer=audio_out_mixer,
        ),
        "plivo": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad_analyzer,
            audio_in_sample_rate=plivo_rate,
            audio_out_sample_rate=plivo_rate,
            audio_out_mixer=audio_out_mixer,
        ),
    }


async def create_plivo_transport(
    websocket: WebSocket,
    params: FastAPIWebsocketParams,
    call_data: dict,
    sample_rate: int = SAMPLE_RATE_LOW,
) -> FastAPIWebsocketTransport:
    """Create Plivo transport with L16 serializer for configurable sample rate.

    Plivo requires a custom L16 serializer for 16 kHz audio because:
    - Default PlivoFrameSerializer uses μ-law which only supports 8 kHz
    - PlivoL16FrameSerializer uses L16 (PCM) which supports 8 kHz and 16 kHz

    Args:
        websocket: FastAPI WebSocket connection
        params: FastAPIWebsocketParams with audio settings
        call_data: Parsed call data containing stream_id and call_id
        sample_rate: Audio sample rate in Hz (8000 or 16000). Default is 8000.

    Returns:
        FastAPIWebsocketTransport configured for Plivo L16 at the specified sample rate
    """
    # Create L16 serializer with the configured sample rate
    serializer = PlivoL16FrameSerializer(
        stream_id=call_data["stream_id"],
        call_id=call_data["call_id"],
        auth_id=PLIVO_AUTH_ID,
        auth_token=PLIVO_AUTH_TOKEN,
        params=PlivoL16FrameSerializer.InputParams(plivo_sample_rate=sample_rate),
    )

    # Set serializer and disable WAV header for telephony
    params.serializer = serializer
    params.add_wav_header = False

    logger.info(f"Creating Plivo transport with L16 serializer at {sample_rate} Hz")

    return FastAPIWebsocketTransport(websocket=websocket, params=params)
