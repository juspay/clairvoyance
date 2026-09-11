"""Telephony transport construction with our own Plivo serializer.

pipecat's ``_create_telephony_transport`` builds the serializer itself and
overwrites ``params.serializer``, so a caller cannot hand it a custom one. For
Plivo we therefore build the transport here — the same two lines pipecat ends
with — using :class:`PlivoL16FrameSerializer` so the call runs wideband.

Every other provider is delegated to pipecat unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import WebSocket
from pipecat.runner.utils import _create_telephony_transport
from pipecat.transports.base_transport import BaseTransport

from app.ai.voice.agents.breeze_buddy.template.vad import TELEPHONY_SAMPLE_RATE
from app.ai.voice.serializers import PlivoL16FrameSerializer
from app.core.logger import logger


async def create_telephony_transport(
    websocket: WebSocket,
    params: Any,
    transport_type: str,
    call_data: Any,
) -> BaseTransport:
    """Build the telephony transport, using L16 wideband audio on Plivo.

    Args:
        websocket: The provider's WebSocket (or our non-closing proxy).
        transport_type: "plivo", "twilio", "exotel" or "telnyx".
        call_data: Parsed handshake data from ``parse_telephony_websocket``.
    """
    if transport_type != "plivo":
        return await _create_telephony_transport(
            websocket, params, transport_type, call_data
        )

    from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport

    # Telephony never wants a WAV header, matching pipecat's own handling.
    params.add_wav_header = False
    params.serializer = PlivoL16FrameSerializer(
        stream_id=call_data["stream_id"],
        call_id=call_data["call_id"],
        auth_id=os.getenv("PLIVO_AUTH_ID", ""),
        auth_token=os.getenv("PLIVO_AUTH_TOKEN", ""),
        params=PlivoL16FrameSerializer.InputParams(
            plivo_sample_rate=TELEPHONY_SAMPLE_RATE,
        ),
    )
    logger.info(f"Plivo transport using L16 wideband at {TELEPHONY_SAMPLE_RATE} Hz")
    return FastAPIWebsocketTransport(websocket=websocket, params=params)
