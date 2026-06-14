"""
WebSocket endpoint for the Daily warm-transfer bridge.

When a Daily-mode lead transfers to a human agent, the AI bot dials the
agent through the lead's telephony provider and writes a Redis bridge flag
keyed by the agent leg's call_sid. The provider's answer webhook (see
``answer/handlers.py``) detects that flag and points the provider's
``<Connect><Stream>`` here instead of the standard AI bot WebSocket.

This endpoint accepts the WebSocket, identifies the call, looks up the
flag, and runs an in-process audio bridge between the telephony WS and the
Daily room. No AI bot is started for this leg.
"""

from fastapi import APIRouter, WebSocket
from pipecat.runner.utils import parse_telephony_websocket
from starlette.websockets import WebSocketDisconnect

from app.ai.voice.agents.breeze_buddy.services.daily.transfer_bridge import (
    run_bridge,
)
from app.ai.voice.agents.breeze_buddy.utils.bridge_flag import (
    STATUS_FAILED,
    get_bridge_flag,
    update_bridge_status,
)
from app.core.logger import logger

router = APIRouter()


@router.websocket("/{service_provider}/bridge/v2")
async def telephony_bridge_handler(service_provider: str, websocket: WebSocket):
    """Run the Daily warm-transfer audio bridge for the dialed agent leg."""
    provider_name = service_provider.lower()
    logger.info(f"[Bridge WS] Connection received for provider {provider_name}")

    await websocket.accept()

    try:
        transport_type, call_data = await parse_telephony_websocket(websocket)
    except Exception as e:
        logger.error(f"[Bridge WS] parse_telephony_websocket failed: {e}")
        await websocket.close(code=4000, reason="Bridge: cannot parse stream")
        return

    call_id = call_data.get("call_id")
    stream_id = call_data.get("stream_id")
    if not call_id or not stream_id:
        logger.error(f"[Bridge WS] Missing call_id/stream_id in handshake: {call_data}")
        await websocket.close(code=4000, reason="Bridge: missing call identifiers")
        return

    flag = await get_bridge_flag(call_id)
    if not flag:
        logger.error(f"[Bridge WS] No bridge flag for call {call_id}; rejecting")
        await websocket.close(code=4004, reason="Bridge: no flag for call")
        return

    expected_provider = flag.get("provider")
    if expected_provider and expected_provider != provider_name:
        logger.error(
            f"[Bridge WS] Provider mismatch for call {call_id}: "
            f"flag={expected_provider} ws={provider_name}"
        )
        await update_bridge_status(
            call_id, STATUS_FAILED, failure_reason="provider_mismatch"
        )
        await websocket.close(code=4003, reason="Bridge: provider mismatch")
        return

    room_url = flag.get("room_url")
    daily_token = flag.get("daily_token")
    if not room_url or not daily_token:
        logger.error(f"[Bridge WS] Flag for {call_id} missing room_url/daily_token")
        await update_bridge_status(
            call_id, STATUS_FAILED, failure_reason="missing_daily_credentials"
        )
        await websocket.close(code=4000, reason="Bridge: missing Daily credentials")
        return

    logger.info(
        f"[Bridge WS] Starting bridge for call {call_id} "
        f"(stream={stream_id}, room={room_url})"
    )

    try:
        await run_bridge(
            websocket=websocket,
            provider=provider_name,
            stream_id=stream_id,
            call_id=call_id,
            room_url=room_url,
            daily_token=daily_token,
        )
    except WebSocketDisconnect:
        logger.info(f"[Bridge WS] Client disconnected for call {call_id}")
    except Exception as e:
        logger.error(
            f"[Bridge WS] Bridge run failed for call {call_id}: {e}",
            exc_info=True,
        )
        try:
            await update_bridge_status(call_id, STATUS_FAILED, failure_reason=str(e))
        except Exception as flag_err:
            logger.warning(
                f"[Bridge WS] Could not update bridge flag for {call_id}: {flag_err}"
            )
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close(code=1011, reason="Bridge: internal error")
        except Exception as close_err:
            logger.warning(
                f"[Bridge WS] Could not close socket for {call_id}: {close_err}"
            )
    finally:
        logger.info(f"[Bridge WS] Connection closed for call {call_id}")
