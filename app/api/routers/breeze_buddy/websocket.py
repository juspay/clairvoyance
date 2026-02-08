from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.ai.voice.agents.breeze_buddy.managers.calls import handle_call_completion
from app.ai.voice.agents.breeze_buddy.services.telephony.utils import get_voice_provider
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.schemas import CallProvider

router = APIRouter()


@router.websocket("/{service_provider}/callback/{template}/v2")
async def telephony_websocket_handler_v2(
    service_provider: str, template: str, websocket: WebSocket
):
    """
    WebSocket endpoint v2 that accepts a connection and passes it to the
    agent.py main function.
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
            error_type = type(e).__name__
            error_message = str(e)
            logger.error(
                f"An error occurred in the WebSocket v2 handler - Type: {error_type}, Message: '{error_message}', Args: {e.args}",
                exc_info=True,
            )
            try:
                if websocket.client_state.name != "DISCONNECTED":
                    await websocket.close(code=1011, reason="Internal Server Error")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket v2 (likely already closed): {close_error}"
                )
        finally:
            logger.info("WebSocket v2 client connection closed.")
