from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.ai.voice.agents.breeze_buddy.managers.calls import handle_call_completion
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    is_caller_disconnected_error,
    is_disconnected,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.schemas import CallProvider

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoints
#
# Pod lifecycle (allocation/release) is managed entirely by Smart Router:
#   - Allocation: telephony webhook → allocate.py → Smart Router HTTP API
#   - Release (primary): status callback → handlers.py → Smart Router release
#   - Release (backup): call completion → calls.py → Smart Router release
#   - Release (safety net): Smart Router zombie cleanup (every 30s)
# ─────────────────────────────────────────────────────────────────────────────


@router.websocket("/{service_provider}/callback/{template}/v2")
async def telephony_websocket_handler_v2(
    service_provider: str, template: str, websocket: WebSocket
):
    """
    WebSocket endpoint v2 that accepts a connection and passes it to the
    agent.py main function.

    Pod allocation is handled upstream by Smart Router before the provider
    connects here. Pod release is handled by status callbacks and call
    completion handlers (both call Smart Router's release endpoint).
    """
    logger.info(f"Handling v2 websocket for {template}")

    async with create_aiohttp_session() as session:
        try:
            provider_enum = CallProvider(service_provider.upper())
            provider = get_voice_provider(provider_enum, session)
            provider.set_completion_callback(handle_call_completion)
            await provider.handle_websocket(websocket, provider_enum)
        except WebSocketDisconnect:
            logger.warning("WebSocket v2 client disconnected.")
        except Exception as e:
            if is_caller_disconnected_error(e):
                # Teardown race that escaped the IVR/audio paths (e.g. during
                # pipeline setup or post-IVR): caller already gone — warn,
                # don't error (same classification as send_message/IVR).
                logger.warning(
                    f"WebSocket v2 handler ended on teardown (caller disconnected): {e}"
                )
            else:
                # .opt(exception=True), NOT exc_info: loguru ignores exc_info, and
                # any kwarg re-.format()s the message — braces in the exception
                # text would raise KeyError from inside this logging call (the
                # 2026-07-28 SSE incident class). No args/kwargs → format() is
                # never called → brace-bearing errors log safely WITH a traceback.
                logger.opt(exception=True).error(
                    f"An error occurred in the WebSocket v2 handler - Type: {type(e).__name__}, "
                    f"Message: '{e}', Args: {e.args}"
                )
            try:
                if not is_disconnected(websocket):
                    await websocket.close(code=1011, reason="Internal Server Error")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket v2 (likely already closed): {close_error}"
                )
        finally:
            logger.info("WebSocket v2 client connection closed.")
