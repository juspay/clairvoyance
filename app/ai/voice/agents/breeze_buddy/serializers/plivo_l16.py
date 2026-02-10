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
                logger.debug(
                    f"[Plivo L16 Serialize] Resampling output from {frame.sample_rate} to {self._plivo_sample_rate}"
                )
                serialized_data = await self._output_resampler.resample(
                    data, frame.sample_rate, self._plivo_sample_rate
                )
            else:
                logger.debug(
                    f"[Plivo L16 Serialize] No resampling needed for output (rate: {frame.sample_rate})"
                )
                serialized_data = data

            if serialized_data is None or len(serialized_data) == 0:
                logger.warning("[Plivo L16 Serialize] Empty audio data, skipping")
                return None

            payload = base64.b64encode(serialized_data).decode("utf-8")
            logger.debug(
                f"[Plivo L16 Serialize] Sending L16 audio: {len(serialized_data)} bytes, "
                f"sample rate: {self._plivo_sample_rate}"
            )
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

        event_type = message.get("event")
        logger.debug(f"[Plivo L16 Deserialize] Received event: {event_type}")

        if event_type == "media":
            media = message.get("media", {})
            content_type = media.get("contentType")
            sample_rate = media.get("sampleRate")
            payload_base64 = media.get("payload")

            logger.debug(
                f"[Plivo L16 Deserialize] Media event - contentType: {content_type}, "
                f"sampleRate: {sample_rate}, payload size: {len(payload_base64) if payload_base64 else 0}"
            )

            if not payload_base64:
                logger.warning("[Plivo L16 Deserialize] No payload in media event")
                return None

            payload = base64.b64decode(payload_base64)
            logger.debug(
                f"[Plivo L16 Deserialize] Decoded payload size: {len(payload)} bytes, "
                f"expected sample rate: {self._plivo_sample_rate}, target: {self._sample_rate}"
            )

            # Input: Plivo sends L16 (PCM) - just resample if needed, no codec conversion
            if self._plivo_sample_rate != self._sample_rate:
                logger.debug(
                    f"[Plivo L16 Deserialize] Resampling from {self._plivo_sample_rate} to {self._sample_rate}"
                )
                deserialized_data = await self._input_resampler.resample(
                    payload,
                    self._plivo_sample_rate,
                    self._sample_rate,
                )
            else:
                logger.debug("[Plivo L16 Deserialize] No resampling needed")
                deserialized_data = payload

            if deserialized_data is None or len(deserialized_data) == 0:
                logger.warning(
                    f"[Plivo L16 Deserialize] Empty data after processing: {deserialized_data is None}"
                )
                return None

            logger.debug(
                f"[Plivo L16 Deserialize] Creating InputAudioRawFrame with {len(deserialized_data)} bytes"
            )
            audio_frame = InputAudioRawFrame(
                audio=deserialized_data, num_channels=1, sample_rate=self._sample_rate
            )
            return audio_frame
        elif event_type == "dtmf":
            dtmf_data = message.get("dtmf", {})
            digit = dtmf_data.get("digit")
            logger.debug(f"[Plivo L16 Deserialize] DTMF event - digit: {digit}")
            if digit:
                try:
                    return InputDTMFFrame(KeypadEntry(digit))
                except ValueError:
                    logger.warning(f"Invalid DTMF digit received: {digit}")
                    return None
        else:
            logger.debug(f"[Plivo L16 Deserialize] Unhandled event type: {event_type}")
            return None
