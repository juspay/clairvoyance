"""Custom Soniox STT service with extended endpoint detection config.

Extends pipecat's SonioxSTTService with:

- ``max_endpoint_delay_ms`` support: controls the maximum delay between end of
  speech and the returned endpoint when using Soniox's native semantic
  endpoint detection. Thin override — removable once pipecat adds native
  support.
- WEBRTC-DIAG probes around the websocket send: logs when speech-energy
  audio is actually handed to Soniox and warns when the base class would drop
  audio silently (websocket not OPEN). These caught the 24k-audio-labeled-16k
  sample-rate bug that made Soniox hear slow-motion speech.
"""

from __future__ import annotations

import json
from typing import AsyncGenerator, Optional

import numpy as np
from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.services.soniox.stt import (
    SonioxContextObject,
    SonioxSTTService,
    _prepare_language_hints,
)
from websockets.asyncio.client import connect as websocket_connect
from websockets.protocol import State


class SonioxSTTServiceWithEndpointDelay(SonioxSTTService):
    """SonioxSTTService with ``max_endpoint_delay_ms`` support.

    When Soniox native endpoint detection is enabled
    (``vad_force_turn_endpoint=False``), this parameter controls the maximum
    delay (in ms) between the end of speech and the returned ``<end>`` token.
    Allowed values: 500–3000 ms. Default from Soniox: 2000 ms.
    """

    # WEBRTC-DIAG send probe: int16 RMS above this counts as speech.
    _DIAG_RMS_THRESHOLD = 400
    # ~0.5s of sub-threshold audio = a quiet gap worth logging after.
    _DIAG_QUIET_SECS = 0.5

    def __init__(
        self,
        *,
        max_endpoint_delay_ms: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._max_endpoint_delay_ms = max_endpoint_delay_ms
        self._diag_quiet_secs = 0.0
        self._diag_drop_logged = False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Wrap the base send with WEBRTC-DIAG logging (latency probes).

        Logs (a) when speech energy is actually handed to the Soniox
        websocket after a quiet gap, and (b) if audio would be silently
        dropped because the websocket isn't OPEN — the base run_stt drops
        without logging. Compare the SENT timestamp against the transport's
        speech-onset log and the interim-transcription observer to locate
        any delay.
        """
        ws_open = self._websocket is not None and self._websocket.state is State.OPEN
        if not ws_open:
            if not self._diag_drop_logged:
                self._diag_drop_logged = True
                logger.warning(
                    "WEBRTC-DIAG soniox ws NOT OPEN — dropping audio silently"
                )
        else:
            self._diag_drop_logged = False
            if audio:
                samples = np.frombuffer(audio, dtype=np.int16)
                rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
                frame_secs = len(audio) / 2 / float(self.sample_rate or 16000)
                if rms >= self._DIAG_RMS_THRESHOLD:
                    if self._diag_quiet_secs >= self._DIAG_QUIET_SECS:
                        logger.info(
                            "WEBRTC-DIAG soniox speech SENT to websocket "
                            f"(rms={rms:.0f}, after {self._diag_quiet_secs:.1f}s quiet)"
                        )
                    self._diag_quiet_secs = 0.0
                else:
                    self._diag_quiet_secs += frame_secs
        async for frame in super().run_stt(audio):
            yield frame

    async def _connect_websocket(self):
        """Override to inject ``max_endpoint_delay_ms`` into the config."""
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            logger.debug("Connecting to Soniox STT (enhanced)")

            self._websocket = await websocket_connect(self._url)

            if not self._websocket:
                await self.push_error(
                    error_msg=f"Unable to connect to Soniox API at {self._url}"
                )
                raise Exception(f"Unable to connect to Soniox API at {self._url}")

            enable_endpoint_detection = not self._vad_force_turn_endpoint

            s = self._settings

            context = s.context
            if isinstance(context, SonioxContextObject):
                context = context.model_dump()

            config = {
                "api_key": self._api_key,
                "model": s.model,
                "audio_format": self._audio_format,
                "num_channels": self._num_channels,
                "enable_endpoint_detection": enable_endpoint_detection,
                "sample_rate": self.sample_rate,
                "language_hints": _prepare_language_hints(
                    s.language_hints if isinstance(s.language_hints, list) else None
                ),
                "language_hints_strict": s.language_hints_strict,
                "context": context,
                "enable_speaker_diarization": s.enable_speaker_diarization,
                "enable_language_identification": s.enable_language_identification,
                "client_reference_id": s.client_reference_id,
            }

            # Inject max_endpoint_delay_ms when native endpoint detection is on
            if enable_endpoint_detection and self._max_endpoint_delay_ms is not None:
                config["max_endpoint_delay_ms"] = self._max_endpoint_delay_ms

            await self._websocket.send(json.dumps(config))

            await self._call_event_handler("on_connected")
            logger.debug("Connected to Soniox STT (enhanced)")
        except Exception as e:
            await self.push_error(
                error_msg=f"Unable to connect to Soniox: {e}", exception=e
            )
            raise
