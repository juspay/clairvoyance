"""SmallWebRTC offer/answer handlers for device and browser test clients.

Validates a WEBRTC lead, then delegates SDP negotiation to pipecat's
SmallWebRTCRequestHandler and spawns the bot IN-PROCESS (the aiortc peer
connection cannot cross a subprocess boundary — see
docs/SMALLWEBRTC_DEVICE_TRANSPORT_SPEC.md).
"""

import asyncio
import re
from typing import List, Optional

from fastapi import HTTPException
from pipecat.runner.types import SmallWebRTCRunnerArguments
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from app.ai.voice.agents.breeze_buddy.agent import webrtc_bot

# Reuse the daily module's completion + live-task tracking: completion just flips
# the lead to FINISHED by call_id==lead_id (nothing Daily-specific), and
# _track_live_bot is the strong-ref set that both counts live bots and prevents
# asyncio GC.
from app.ai.voice.agents.breeze_buddy.services.daily.daily import (
    _live_bot_tasks,
    _track_live_bot,
    daily_completion_function,
)
from app.core.config.static import (
    BB_MAX_CONCURRENT_WEBRTC_BOTS,
    BB_WEBRTC_ESP32_HOST,
    BB_WEBRTC_ICE_SERVERS,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session

# get_lead_by_id is imported from the canonical accessor (not daily.handlers,
# which drags in JWT-secret-requiring security modules at import time). Tests
# patch this name on THIS module.
from app.database.accessor import get_lead_by_id
from app.schemas.breeze_buddy.core import ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.smallwebrtc import (
    SmallWebRTCAnswerResponse,
    SmallWebRTCOfferRequest,
    SmallWebRTCPatchRequestBody,
    SmallWebRTCPatchResponse,
)


def parse_ice_servers(raw: str) -> List[IceServer]:
    """Parse a comma-separated BB_WEBRTC_ICE_SERVERS string into IceServers.

    Supported per-entry forms (blank/whitespace entries ignored; empty string
    yields []):
      - stun:host:port
      - turn:USER:PASS@host:port[?transport=udp]   (also turns:)  -> creds split out
      - turn:host:port                              (no embedded credentials)

    IceServer is aiortc's RTCIceServer(urls, username=None, credential=None);
    aiortc wants the URL WITHOUT embedded credentials and the user/pass passed
    separately, so we strip the `USER:PASS@` prefix into username/credential.
    """
    servers: List[IceServer] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        scheme, sep, rest = entry.partition(":")
        if sep and scheme.lower() in ("turn", "turns") and "@" in rest:
            creds, _, hostpart = rest.partition("@")
            user, _, password = creds.partition(":")
            servers.append(
                IceServer(
                    urls=f"{scheme}:{hostpart}",
                    username=user or None,
                    credential=password or None,
                )
            )
        else:
            # stun:, or turn/turns without embedded credentials — URL only.
            servers.append(IceServer(urls=entry))
    return servers


_ice_servers = parse_ice_servers(BB_WEBRTC_ICE_SERVERS)

# Handler registry. ESP32 SDP munging is a property of the CONNECTING CLIENT (it
# self-identifies with client_type: "esp32" in the offer body), never of the
# deployment or the template — the same template serves both a browser test (no
# munging) and a device (munging). pipecat fixes esp32_mode/host at handler
# construction, so we keep one default handler and lazily create one munging
# handler per host on first ESP32 request. Any future device type works with
# zero env/template changes: it just declares its client_type.
_default_handler = SmallWebRTCRequestHandler(
    ice_servers=_ice_servers,
    connection_mode=ConnectionMode.MULTIPLE,
)
_esp32_handlers: dict[str, SmallWebRTCRequestHandler] = {}

# The munging host may be derived from the client-controlled Host header, so
# cache creation must be bounded (an attacker rotating Host values must not be
# able to grow this dict without limit) and the value must at least look like a
# hostname/IP before we build a handler around it. Legitimate deployments see a
# handful of hosts at most (usually exactly one).
_MAX_ESP32_HANDLERS = 8
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _get_handler(
    client_type: Optional[str], request_host: Optional[str]
) -> SmallWebRTCRequestHandler:
    if client_type != "esp32":
        return _default_handler
    # Munging host: env override wins (proxies/LBs may rewrite Host); else the
    # address the device actually dialed.
    host = BB_WEBRTC_ESP32_HOST or (request_host or "").split(":")[0]
    if not host or not _HOSTNAME_RE.match(host):
        logger.warning(
            f"ESP32 client with missing/invalid munging host {host!r} — "
            "using default handler (no SDP munging)"
        )
        return _default_handler
    if host not in _esp32_handlers:
        if len(_esp32_handlers) >= _MAX_ESP32_HANDLERS:
            logger.warning(
                f"ESP32 handler cache full ({_MAX_ESP32_HANDLERS}); refusing new "
                f"munging host {host!r} — using default handler. Set "
                "BB_WEBRTC_ESP32_HOST to pin the correct host."
            )
            return _default_handler
        _esp32_handlers[host] = SmallWebRTCRequestHandler(
            ice_servers=_ice_servers,
            connection_mode=ConnectionMode.MULTIPLE,
            esp32_mode=True,
            host=host,
        )
    return _esp32_handlers[host]


def _all_handlers() -> List[SmallWebRTCRequestHandler]:
    return [_default_handler, *_esp32_handlers.values()]


async def validate_webrtc_lead(lead_id: str):
    lead = await get_lead_by_id(lead_id)
    if not lead:
        raise HTTPException(
            status_code=404, detail=f"Lead not found with id: {lead_id}"
        )
    if lead.execution_mode not in (ExecutionMode.WEBRTC, ExecutionMode.WEBRTC_TEST):
        raise HTTPException(
            status_code=400,
            detail=f"Lead is not a WebRTC lead. execution_mode: {lead.execution_mode}",
        )
    if lead.status != LeadCallStatus.BACKLOG:
        raise HTTPException(
            status_code=400, detail=f"Lead already processed. status: {lead.status}"
        )
    return lead


# Offers that passed the concurrency check but whose bot task hasn't been
# created yet (handle_web_request awaits ICE gathering before invoking the
# connection callback). Counted alongside _live_bot_tasks so concurrent offers
# can't all pass the limit check before any of them registers a task (TOCTOU).
# Single event loop + no awaits between check and increment => no lock needed.
_pending_bot_starts = 0


async def smallwebrtc_offer_handler(
    offer: SmallWebRTCOfferRequest, request_host: Optional[str] = None
) -> SmallWebRTCAnswerResponse:
    global _pending_bot_starts

    lead_id = offer.resolved_lead_id()
    if not lead_id:
        raise HTTPException(
            status_code=400,
            detail="lead_id is required (top-level or requestData.lead_id)",
        )

    # pc_id present = renegotiation/reconnect of an existing live connection —
    # skip lead validation (the lead is legitimately PROCESSING by then) and
    # don't reserve a bot slot (the bot already exists).
    reserved = False
    if not offer.pc_id:
        await validate_webrtc_lead(lead_id)
        if len(_live_bot_tasks) + _pending_bot_starts >= BB_MAX_CONCURRENT_WEBRTC_BOTS:
            raise HTTPException(
                status_code=503, detail="Too many concurrent WebRTC sessions"
            )
        _pending_bot_starts += 1
        reserved = True

    async def _on_connection(conn: object) -> None:
        nonlocal reserved
        runner_args = SmallWebRTCRunnerArguments(webrtc_connection=conn)
        runner_args.body = {"lead_id": lead_id}
        session = create_aiohttp_session()
        task = asyncio.create_task(
            webrtc_bot(runner_args, daily_completion_function, session)
        )
        _track_live_bot(task)
        if reserved:
            global _pending_bot_starts
            _pending_bot_starts -= 1
            reserved = False
        logger.info(f"Spawned in-process SmallWebRTC bot for lead_id: {lead_id}")

    try:
        handler = _get_handler(offer.resolved_client_type(), request_host)
        # Build pipecat's request explicitly: SmallWebRTCRequest is a dataclass
        # (cls(**data)) that rejects unknown keys, so passing our model's extra
        # fields (lead_id/client_type) through would raise TypeError.
        request = SmallWebRTCRequest(
            sdp=offer.sdp,
            type=offer.type,
            pc_id=offer.pc_id,
            restart_pc=offer.restart_pc,
            request_data=offer.request_data,
        )
        answer = await handler.handle_web_request(request, _on_connection)
    finally:
        # Reservation still held => the connection callback never ran (SDP
        # negotiation failed or handler raised): release the slot.
        if reserved:
            _pending_bot_starts -= 1

    if answer is None:
        raise HTTPException(
            status_code=500, detail="SmallWebRTC handler produced no SDP answer"
        )
    return SmallWebRTCAnswerResponse(**answer)


async def smallwebrtc_patch_handler(
    body: SmallWebRTCPatchRequestBody,
) -> SmallWebRTCPatchResponse:
    # Route the PATCH to the handler that owns this pc_id (ICE trickle /
    # renegotiation must reach the same peer-connection map that answered the
    # offer). _pcs_map is a private attr but stable in the pinned pipecat.
    patch = SmallWebRTCPatchRequest(
        pc_id=body.pc_id,
        candidates=[
            IceCandidate(
                candidate=c.candidate,
                sdp_mid=c.sdp_mid,
                sdp_mline_index=c.sdp_mline_index,
            )
            for c in body.candidates
        ],
    )
    for handler in _all_handlers():
        if body.pc_id in handler._pcs_map:
            await handler.handle_patch_request(patch)
            return SmallWebRTCPatchResponse()
    raise HTTPException(status_code=404, detail=f"Unknown pc_id: {body.pc_id}")


async def close_smallwebrtc_handler() -> None:
    """Lifespan shutdown: close all live peer connections on every handler."""
    for handler in _all_handlers():
        await handler.close()
