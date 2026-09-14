"""Plivo serializer for wideband L16 audio.

pipecat's ``PlivoFrameSerializer`` is hardcoded to G.711 mu-law, which the ITU
defines only at 8 kHz — Plivo therefore offers ``audio/x-mulaw`` at 8000 Hz and
nothing else. Wideband is available exclusively as ``audio/x-l16`` (8/16/24 kHz).

A 4G (VoLTE) call carries the caller's voice at 16 kHz all the way to Plivo, so
asking Plivo for mu-law throws away half the audio bandwidth before STT ever
sees it — losing exactly the 4–8 kHz range that separates "S" from "F" and makes
dictated digits ambiguous. This subclass keeps the audio at 16 kHz.

L16 is raw little-endian PCM, which is what the pipeline already speaks, so both
directions here are the parent's logic minus the mu-law conversion.
"""

from __future__ import annotations

import base64
import json

from pipecat.frames.frames import AudioRawFrame, Frame, InputAudioRawFrame
from pipecat.serializers.plivo import PlivoFrameSerializer

from app.core.logger import logger

L16_CONTENT_TYPE = "audio/x-l16"


class PlivoL16FrameSerializer(PlivoFrameSerializer):
    """``PlivoFrameSerializer`` speaking ``audio/x-l16`` instead of mu-law.

    The ``<Stream>`` ``contentType`` (inbound) and each ``playAudio`` message
    (outbound) are negotiated separately by Plivo, but we set both to L16 so the
    call runs at one rate end to end.

    ``plivo_sample_rate`` must match the rate requested in the ``<Stream>`` XML.
    """

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Turn a Plivo ``media`` event into an input audio frame.

        The parent runs ``ulaw_to_pcm`` here. L16 is already PCM, so the payload
        only needs un-base64-ing. Every non-media event (dtmf, start, stop) is
        left to the parent so its stream-id capture and hang-up logic keep working.
        """
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return await super().deserialize(data)

        if message.get("event") != "media":
            return await super().deserialize(data)

        payload_base64 = message.get("media", {}).get("payload")
        if not payload_base64:
            return None

        pcm = base64.b64decode(payload_base64)
        if not pcm:
            return None

        # Plivo's rate and the pipeline's rate are configured to match, so no
        # resampling is needed; if they ever diverge, that is a config bug worth
        # surfacing rather than silently papering over.
        if self._sample_rate and self._sample_rate != self._plivo_sample_rate:
            logger.warning(
                f"PlivoL16FrameSerializer: pipeline rate {self._sample_rate} != "
                f"Plivo rate {self._plivo_sample_rate}; audio will play at the "
                "wrong speed. Align TELEPHONY_SAMPLE_RATE with the <Stream> contentType."
            )

        return InputAudioRawFrame(
            audio=pcm,
            num_channels=1,
            sample_rate=self._plivo_sample_rate,
        )

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Turn an output audio frame into a Plivo ``playAudio`` message.

        The parent runs ``pcm_to_ulaw``; L16 needs no conversion. Non-audio
        frames (EndFrame hang-up, interruptions, DTMF) stay with the parent.
        """
        if not isinstance(frame, AudioRawFrame):
            return await super().serialize(frame)

        if not frame.audio:
            return None

        return json.dumps(
            {
                "event": "playAudio",
                "media": {
                    "contentType": L16_CONTENT_TYPE,
                    "sampleRate": self._plivo_sample_rate,
                    "payload": base64.b64encode(frame.audio).decode("utf-8"),
                },
                "streamId": self._stream_id,
            }
        )
