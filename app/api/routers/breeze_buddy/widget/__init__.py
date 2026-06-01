"""Unified widget endpoints (CHAT_MODE.md §14).

Six routes under ``/agent/voice/breeze-buddy/widget``:

- ``POST /widget/session``                              create
- ``POST /widget/session/{id}/message``                 chat turn (SSE)
- ``POST /widget/session/{id}/voice/connect``           open voice attachment
- ``POST /widget/session/{id}/voice/end``               close voice attachment
- ``POST /widget/session/{id}/end``                     end whole conversation
- ``GET  /widget/session/{id}``                         resume state

Plus an OPTIONS handler per route returning permissive CORS so the
browser preflight from any merchant origin can reach our handlers —
the per-merchant origin allowlist is enforced inside the POST handlers
(application layer), not via global CORSMiddleware (transport layer).

The first route uses the ``public_widget_key`` (from the embed) +
Origin + per-IP rate limit. All other routes use the session-bound
``widget_token`` minted at create-time.
"""

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.routers.breeze_buddy.widget_common import options_cors_response
from app.api.security.breeze_buddy.widget_token import (
    WidgetSessionContext,
    require_widget_session,
)
from app.schemas.breeze_buddy.chat import (
    CreateWidgetSessionRequest,
    CreateWidgetSessionResponse,
    EndChatSessionResponse,
    SendChatMessageRequest,
    WidgetSessionStateResponse,
    WidgetVoiceConnectResponse,
    WidgetVoiceEndResponse,
)

from .handlers import (
    cancel_widget_message_handler,
    create_widget_session_handler,
    end_widget_session_handler,
    get_widget_session_state_handler,
    send_widget_message_handler,
    voice_connect_handler,
    voice_end_handler,
)

router = APIRouter(prefix="/widget", tags=["widget-session"])


# ---------------------------------------------------------------------------
# CORS preflight (transport layer — application allowlist runs in handlers)
# ---------------------------------------------------------------------------


@router.options("/session")
async def widget_create_preflight() -> Response:
    return options_cors_response()


@router.options("/session/{session_id}")
async def widget_get_preflight(session_id: str) -> Response:
    return options_cors_response()


@router.options("/session/{session_id}/message")
async def widget_message_preflight(session_id: str) -> Response:
    return options_cors_response()


@router.options("/session/{session_id}/cancel")
async def widget_cancel_preflight(session_id: str) -> Response:
    return options_cors_response()


@router.options("/session/{session_id}/voice/connect")
async def widget_voice_connect_preflight(session_id: str) -> Response:
    return options_cors_response()


@router.options("/session/{session_id}/voice/end")
async def widget_voice_end_preflight(session_id: str) -> Response:
    return options_cors_response()


@router.options("/session/{session_id}/end")
async def widget_end_preflight(session_id: str) -> Response:
    return options_cors_response()


# ---------------------------------------------------------------------------
# Public — auth via public_widget_key + Origin + per-IP rate limit
# ---------------------------------------------------------------------------


@router.post(
    "/session",
    response_model=CreateWidgetSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a widget session + mint widget_token",
)
async def create_widget_session(
    body: CreateWidgetSessionRequest, request: Request
) -> CreateWidgetSessionResponse:
    return await create_widget_session_handler(body, request)


# ---------------------------------------------------------------------------
# Authenticated by widget_token (session-bound)
# ---------------------------------------------------------------------------


@router.post(
    "/session/{session_id}/message",
    summary="Stream one widget chat turn (SSE)",
)
async def send_widget_message(
    session_id: str,
    req: SendChatMessageRequest,
    request: Request,
    ctx: WidgetSessionContext = Depends(require_widget_session),
):
    return await send_widget_message_handler(session_id, req, request, ctx)


@router.post(
    "/session/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel an in-flight widget chat turn (Stop button)",
)
async def cancel_widget_message(
    session_id: str,
    ctx: WidgetSessionContext = Depends(require_widget_session),
) -> Response:
    """Cancel the in-flight ``/message`` stream for ``session_id``.

    Best-effort + idempotent — always returns 202. The owning pod
    cancels its asyncio task; the stream's ``finally`` releases the
    per-session Redis lock so the next ``/message`` can proceed
    immediately. If no turn is in flight, this is a harmless no-op.
    """
    await cancel_widget_message_handler(session_id, ctx)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/session/{session_id}/voice/connect",
    response_model=WidgetVoiceConnectResponse,
    summary="Open a voice attachment on this widget session",
)
async def voice_connect(
    session_id: str,
    request: Request,
    ctx: WidgetSessionContext = Depends(require_widget_session),
) -> WidgetVoiceConnectResponse:
    return await voice_connect_handler(session_id, request, ctx)


@router.post(
    "/session/{session_id}/voice/end",
    response_model=WidgetVoiceEndResponse,
    summary="Close the voice attachment (best-effort, idempotent)",
)
async def voice_end(
    session_id: str,
    ctx: WidgetSessionContext = Depends(require_widget_session),
) -> WidgetVoiceEndResponse:
    return await voice_end_handler(session_id, ctx)


@router.post(
    "/session/{session_id}/end",
    response_model=EndChatSessionResponse,
    summary="End the whole widget conversation",
)
async def end_widget_session(
    session_id: str,
    ctx: WidgetSessionContext = Depends(require_widget_session),
) -> EndChatSessionResponse:
    return await end_widget_session_handler(session_id, ctx)


@router.get(
    "/session/{session_id}",
    response_model=WidgetSessionStateResponse,
    summary="Resume payload for the widget after a page reload",
)
async def get_widget_session_state(
    session_id: str,
    ctx: WidgetSessionContext = Depends(require_widget_session),
) -> WidgetSessionStateResponse:
    return await get_widget_session_state_handler(session_id, ctx)


__all__ = ["router"]
