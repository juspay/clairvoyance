"""Custom Soniox STT service with extended endpoint detection config.

Extends pipecat's SonioxSTTService to support ``max_endpoint_delay_ms`` which
controls the maximum delay between end of speech and the returned endpoint when
using Soniox's native semantic endpoint detection.

This is a thin override — once pipecat adds native support for
``max_endpoint_delay_ms`` this module can be removed.
"""

from __future__ import annotations

import json
from typing import Optional

from loguru import logger
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

    def __init__(
        self,
        *,
        max_endpoint_delay_ms: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._max_endpoint_delay_ms = max_endpoint_delay_ms

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
