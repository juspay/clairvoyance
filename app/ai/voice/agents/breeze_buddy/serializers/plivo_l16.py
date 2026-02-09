"""Custom Plivo serializer for L16 (PCM) audio format at 16 kHz.

Plivo supports two audio formats:
- audio/x-mulaw: Only 8 kHz sample rate
- audio/x-l16 (PCM): 8 kHz or 16 kHz sample rates

For 16 kHz audio, we must use L16 format, not μ-law.
"""

import base64
import json
from typing import Optional

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.serializers.plivo import PlivoFrameSerializer

from app.core.logger import logger


class PlivoL16FrameSerializer(PlivoFrameSerializer):
    """Custom Plivo serializer for L16 (PCM) audio format.

    Extends PlivoFrameSerializer to handle L16 (Linear PCM) instead of μ-law.
    This is required for 16 kHz audio as Plivo only supports μ-law at 8 kHz.
    """

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Serializes a Pipecat frame to Plivo WebSocket format using L16 (PCM).

        Args:
            frame: The Pipecat frame to serialize.

        Returns:
            Serialized data as string or bytes, or None if the frame isn't handled.
        """
        if (
            self._params.auto_hang_up
            and not self._hangup_attempted
            and isinstance(frame, (EndFrame, CancelFrame))
        ):
            self._hangup_attempted = True
            await self._hang_up_call()
            return None
        elif isinstance(frame, InterruptionFrame):
            answer = {"event": "clearAudio", "streamId": self._stream_id}
            return json.dumps(answer)
        elif isinstance(frame, AudioRawFrame):
            data = frame.audio

            # Output: Resample PCM to Plivo's sample rate (no codec conversion for L16)
            if frame.sample_rate != self._plivo_sample_rate:
                serialized_data = await self._output_resampler.resample(
                    data, frame.sample_rate, self._plivo_sample_rate
                )
            else:
                serialized_data = data

            if serialized_data is None or len(serialized_data) == 0:
                return None

            payload = base64.b64encode(serialized_data).decode("utf-8")
            answer = {
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-l16",  # L16 (PCM) format, not μ-law
                    "sampleRate": self._plivo_sample_rate,
                    "payload": payload,
                },
                "streamId": self._stream_id,
            }

            return json.dumps(answer)
        elif isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
            return json.dumps(frame.message)

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserializes Plivo WebSocket data to Pipecat frames (L16 format).

        Args:
            data: The raw WebSocket data from Plivo.

        Returns:
            A Pipecat frame corresponding to the Plivo event, or None if unhandled.
        """
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON message: {data}")
            return None

        if message.get("event") == "media":
            media = message.get("media", {})
            payload_base64 = media.get("payload")

            if not payload_base64:
                return None

            payload = base64.b64decode(payload_base64)

            # Input: Plivo sends L16 (PCM) - just resample if needed, no codec conversion
            if self._plivo_sample_rate != self._sample_rate:
                deserialized_data = await self._input_resampler.resample(
                    payload,
                    self._plivo_sample_rate,
                    self._sample_rate,
                )
            else:
                deserialized_data = payload

            if deserialized_data is None or len(deserialized_data) == 0:
                return None

            audio_frame = InputAudioRawFrame(
                audio=deserialized_data, num_channels=1, sample_rate=self._sample_rate
            )
            return audio_frame
        elif message.get("event") == "dtmf":
            dtmf_data = message.get("dtmf", {})
            digit = dtmf_data.get("digit")
            if digit:
                try:
                    return InputDTMFFrame(KeypadEntry(digit))
                except ValueError:
                    logger.warning(f"Invalid DTMF digit received: {digit}")
                    return None
        else:
            return None
