"""
Chat (text-mode) endpoints for Breeze Buddy.

Five endpoints under ``/agent/voice/breeze-buddy/chat`` (the actual
breeze_buddy mount per ``app/main.py``; ``CHAT_MODE.md §6`` shows the
logical ``/api/breeze_buddy/chat`` namespace):

- ``POST /chat/session`` — create a new chat session row, persist
  the rendered ``initial_greeting`` (if the template has one), return it.
- ``GET  /chat/session/{id}`` — full session state for resume.
- ``POST /chat/session/{id}/message`` — drive one user→assistant
  turn, stream events back as Server-Sent Events.
- ``POST /chat/session/{id}/end`` — mark the session ENDED.
- ``GET  /chat/session/{id}/transcript`` — read-only transcript
  export.

Architecture: stateless per turn. Each ``POST /message`` constructs a
fresh ``ChatAgent``, replays history from the DB, drives one turn,
and tears down. No in-memory registry, no sticky LB, no lock
auto-extension — a single turn fits comfortably inside the 180s lock
TTL. Idle session cleanup is owned by the global
``BackgroundTaskScheduler``. See ``CHAT_MODE.md §11`` for the
multi-pod model.

For module layout this mirrors the leads router:
- ``__init__.py`` — thin route declarations: auth + RBAC + delegation.
- ``handlers.py`` — DB / lock / agent business logic.
- ``rbac.py`` — reseller / merchant access validators.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.chat import (
    ApproveToolRequest,
    ChatSessionStatus,
    ChatTranscriptResponse,
    CreateChatSessionRequest,
    CreateChatSessionResponse,
    EndChatSessionResponse,
    GetChatSessionResponse,
    ListChatSessionsResponse,
    SendChatMessageRequest,
)

from .demo import router as demo_router
from .handlers import (
    approve_chat_tool_handler,
    cancel_chat_turn_handler,
    create_chat_session_handler,
    end_chat_session_handler,
    get_chat_session_handler,
    get_chat_transcript_handler,
    list_chat_sessions_handler,
    load_chat_session_or_404,
    send_chat_message_handler,
    validate_template_for_chat,
)
from .rbac import validate_chat_create_access, validate_chat_session_access

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/session",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateChatSessionResponse,
)
async def create_session(
    req: CreateChatSessionRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> CreateChatSessionResponse:
    """
    Create a new chat session for a chat-enabled template.

    The session is pure DB state at this point — no agent, no
    pipeline. The first ``POST /message`` builds the agent on demand.
    If the template defines ``configurations.initial_greeting``, it is
    rendered with ``req.template_vars``, persisted as the first
    assistant message, and returned in the response.

    RBAC:
    - Admin: Can create a session for any reseller/merchant
    - Reseller / Merchant: Must have access to the template's
      reseller (and merchant if scoped)

    Returns 400 if the template doesn't list ``"chat"`` in
    ``supported_channels`` or uses a realtime/S2S LLM (voice-only).
    """
    template = await get_template_by_id_cached(req.template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{req.template_id}' not found",
        )

    validate_chat_create_access(
        current_user,
        reseller_id=template.reseller_id,
        merchant_id=getattr(template, "merchant_id", None),
        operation="create chat session",
    )
    validate_template_for_chat(template)

    return await create_chat_session_handler(req, template, current_user)


@router.get("/sessions", response_model=ListChatSessionsResponse)
async def list_sessions(
    template_id: Optional[str] = Query(
        default=None, description="Filter to one agent/template."
    ),
    merchant_id: Optional[str] = Query(
        default=None,
        description=(
            "Narrow to one merchant (workspace). Admins use it to view a "
            "single workspace; scoped users are already limited by RBAC."
        ),
    ),
    reseller_id: Optional[str] = Query(
        default=None,
        description=(
            "Narrow to one reseller (all its merchants). Admins use it to "
            "view a reseller umbrella; scoped users are limited by RBAC."
        ),
    ),
    status_filter: Optional[ChatSessionStatus] = Query(
        default=None, alias="status", description="Filter by session status."
    ),
    date_from: Optional[datetime] = Query(
        default=None, description="Created at >= this ISO-8601 timestamp."
    ),
    date_to: Optional[datetime] = Query(
        default=None, description="Created at < this ISO-8601 timestamp."
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ListChatSessionsResponse:
    """
    List chat sessions for the conversational-log view, most-recently-active
    first. Filter by template (agent), status, and created-at date range;
    paginated.

    RBAC:
    - Admin: sees all sessions (optionally filtered).
    - Reseller / Merchant: scoped to their accessible resellers / merchants
      (a user with no assignments gets 403).

    The per-session transcript (with rendered UI + per-turn latency) is
    fetched separately via ``GET /session/{id}/transcript``.
    """
    return await list_chat_sessions_handler(
        current_user,
        template_id=template_id,
        merchant_id=merchant_id,
        reseller_id=reseller_id,
        session_status=status_filter,
        date_from=date_from,
        date_to=date_to,
        page=page,
        limit=limit,
    )


@router.get("/session/{session_id}", response_model=GetChatSessionResponse)
async def get_session(
    session_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> GetChatSessionResponse:
    """
    Get a chat session's current state + full message history.

    Used by clients on resume (e.g., page reload) to rebuild the UI.

    RBAC:
    - Admin: Can read any session
    - Reseller / Merchant: Must own the session's reseller / merchant
      (returns 404 to avoid leaking existence)
    """
    session = await load_chat_session_or_404(session_id)
    validate_chat_session_access(current_user, session, operation="get_session")
    return await get_chat_session_handler(session)


@router.post("/session/{session_id}/message")
async def send_message(
    session_id: str,
    req: SendChatMessageRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Drive one user→assistant turn; stream Server-Sent Events back to
    the client until ``turn_end``.

    Multi-pod safe: a per-session Redis lock guarantees one driver at
    a time. Lock contention surfaces as ``409 Conflict`` before any
    SSE bytes are written.

    RBAC is enforced inside the handler under the lock to keep the
    happy path to a single session read. See
    ``send_chat_message_handler`` for the rationale.

    Returns:
        ``StreamingResponse`` (``text/event-stream``) of SSE events.
        404 if session id is unknown, 410 if already ENDED, 409 if
        contended.
    """

    # Closure-build the access check so the handler stays auth-agnostic
    # (the demo router passes ``access_check=None`` — the demo token is
    # already bound to a specific session_id and there's nothing further
    # to authorise).
    def _check(session) -> None:
        validate_chat_session_access(current_user, session, operation="send_message")

    return await send_chat_message_handler(session_id, req, access_check=_check)


@router.post("/session/{session_id}/approval")
async def approve_tool(
    session_id: str,
    req: ApproveToolRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Apply a human decision to a pending HITL tool approval and stream
    the resumed turn (same SSE shape as ``/message``).

    The turn that requested approval ended with ``turn_end
    {awaiting_approval: true}``; this endpoint atomically claims the
    pending row, executes (approve) or records a denial (deny / late
    decision past expiry → timeout), and continues the LLM loop.

    Note: like ``/message`` on this RBAC surface, there is no
    current_channel gate — accepted asymmetry with the widget surface.

    Returns:
        ``StreamingResponse`` (``text/event-stream``). 404 if the session
        or tool_call_id is unknown, 410 if the session ENDED, 409 with a
        machine-readable ``detail.code`` (``already_decided`` |
        ``lock_contended``) on conflicts.
    """

    def _check(session) -> None:
        validate_chat_session_access(current_user, session, operation="approve_tool")

    return await approve_chat_tool_handler(session_id, req, access_check=_check)


@router.post(
    "/session/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel an in-flight chat turn (Stop button)",
)
async def cancel_turn(
    session_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Response:
    """Cancel the in-flight ``/message`` SSE stream for ``session_id``.

    Best-effort and idempotent — returns 202 regardless of whether a
    turn was actually running. The owning pod cancels its asyncio task;
    the stream's ``finally`` releases the per-session Redis lock so the
    next ``/message`` can proceed immediately instead of waiting on
    the lock TTL.

    RBAC:
    - Admin: Can cancel any session
    - Reseller / Merchant: Must own the session (404 otherwise)

    The earlier "auth-only" model (skip the session lookup, treat cancel
    as harmless) was a DoS / griefing vector — any authenticated user
    could cancel any tenant's in-flight turn by guessing/leaking session
    UUIDs. The cost of one indexed SELECT is well under the LLM round-trip
    we're cancelling.
    """
    session = await load_chat_session_or_404(session_id)
    validate_chat_session_access(current_user, session, operation="cancel_turn")
    await cancel_chat_turn_handler(session_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/session/{session_id}/end", response_model=EndChatSessionResponse)
async def end_session(
    session_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> EndChatSessionResponse:
    """
    Mark the chat session ENDED. Idempotent.

    Acquires the per-session Redis lock so a concurrent
    ``send_message`` doesn't race with the end mutation.

    RBAC:
    - Admin: Can end any session
    - Reseller / Merchant: Must own the session (404 otherwise)
    """
    session = await load_chat_session_or_404(session_id)
    validate_chat_session_access(current_user, session, operation="end_session")
    return await end_chat_session_handler(session_id, session)


@router.get(
    "/session/{session_id}/transcript",
    response_model=ChatTranscriptResponse,
)
async def get_transcript(
    session_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ChatTranscriptResponse:
    """
    Read-only transcript export for audit / handoff.

    RBAC:
    - Admin: Can export any transcript
    - Reseller / Merchant: Must own the session (404 otherwise)
    """
    session = await load_chat_session_or_404(session_id)
    validate_chat_session_access(current_user, session, operation="get_transcript")
    return await get_chat_transcript_handler(session)


# Public demo subrouter (CHAT_MODE.md §13). Mounted under ``/chat/demo``;
# none of these routes use ``get_current_user_with_rbac`` — the demo
# token + IP rate-limit live inside ``demo.py``. Kept on a sub-prefix so
# anything not explicitly opted-in via ``DEMO_TEMPLATES`` stays gated by
# the standard RBAC routes above.
router.include_router(demo_router, prefix="/demo", tags=["chat-demo"])

__all__ = ["router"]
