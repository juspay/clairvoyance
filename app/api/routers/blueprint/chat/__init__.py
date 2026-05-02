"""Blueprint chat endpoints.

Endpoints:
- POST /sessions/{session_id}/messages        - Send a message (SSE streaming)
- POST /sessions/{session_id}/messages/sync   - Send a message (wait for full response)
"""

from fastapi import APIRouter, Depends

from app.api.routers.blueprint.rbac import get_blueprint_user
from app.schemas import UserInfo
from app.schemas.blueprint.chat import SendMessageRequest, SendMessageResponse

from .handlers import (
    send_message_handler,
    stream_message_handler,
)

router = APIRouter()


@router.post("/sessions/{session_id}/messages")
async def stream_message(
    session_id: str,
    request: SendMessageRequest,
    current_user: UserInfo = Depends(get_blueprint_user),
):
    """Send a message to Blueprint and stream the response via SSE."""
    return await stream_message_handler(session_id, request, current_user)


@router.post(
    "/sessions/{session_id}/messages/sync",
    response_model=SendMessageResponse,
)
async def send_message(
    session_id: str,
    request: SendMessageRequest,
    current_user: UserInfo = Depends(get_blueprint_user),
):
    """Send a message to Blueprint (non-streaming, full response)."""
    return await send_message_handler(session_id, request, current_user)
