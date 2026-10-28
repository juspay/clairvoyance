"""The Inbox's routes — root-mounted at /conversations (ADR 0022). Thin:
each route declares the tenancy door, turns the caller into an Actor and
calls the logic; status codes come from TranslatingRoute alone."""

from typing import Any, Callable, Coroutine, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.auth import merchant_scope
from app.crm.conversations import realtime, reply, threads
from app.crm.conversations.access import actor_of
from app.crm.conversations.errors import (
    ConversationError,
    NotAllowed,
    ThreadConflict,
    ThreadNotFound,
)
from app.crm.conversations.schemas import (
    AssignRequest,
    NoteRequest,
    ReplyRead,
    ReplyRequest,
    Thread,
    ThreadAction,
    ThreadDetail,
    ThreadPage,
    TimelinePage,
    TimelineRow,
)
from app.crm.conversations.status import VIEW_ALL
from app.schemas import UserInfo

COMPONENT = "crm.conversations"


def translate(error: Exception) -> Optional[HTTPException]:
    """PURE: conversations' refusals -> the one status code each earns."""
    if isinstance(error, ThreadNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, NotAllowed):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    if isinstance(error, ThreadConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, ValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.errors(include_input=False),
        )
    if isinstance(error, ConversationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
    return None


class TranslatingRoute(APIRoute):
    """The one place this module's exceptions become status codes."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _translated(request: Request) -> Response:
            try:
                return await handler(request)
            except Exception as error:  # noqa: BLE001 — re-raised unless ours
                translated = translate(error)
                if translated is None:
                    raise
                raise translated from error

        return _translated


router = APIRouter(route_class=TranslatingRoute)


def _scope(operation: str):
    return Depends(merchant_scope(operation, COMPONENT))


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@router.get("", response_model=ThreadPage)
async def list_route(
    view: str = Query(VIEW_ALL),
    channel: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    cursor: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
    merchant_id: str = _scope("list conversations"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ThreadPage:
    """A view of the merchant's conversations, newest first, with counts."""
    return await threads.list_threads(
        merchant_id, actor_of(current_user), view, channel, q, cursor, limit
    )


@router.get("/stream")
async def stream_route(
    merchant_id: str = _scope("watch conversations"),
) -> StreamingResponse:
    """Server-Sent Events: a ``thread`` event {id, kind} whenever one of the
    merchant's conversations changes. Wake-ups only — re-read what changed."""
    return StreamingResponse(
        realtime.stream(merchant_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{thread_id}", response_model=ThreadDetail)
async def detail_route(
    thread_id: UUID, merchant_id: str = _scope("read a conversation")
) -> ThreadDetail:
    return await threads.thread_detail(merchant_id, str(thread_id))


@router.get("/{thread_id}/messages", response_model=TimelinePage)
async def messages_route(
    thread_id: UUID,
    before: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    merchant_id: str = _scope("read a conversation"),
) -> TimelinePage:
    return await threads.timeline(merchant_id, str(thread_id), before, limit)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


@router.post("/{thread_id}/claim", response_model=Thread)
async def claim_route(
    thread_id: UUID,
    req: ThreadAction,
    merchant_id: str = _scope("take over a conversation"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Thread:
    """Take over: 409 when someone else already holds it."""
    return await threads.take_over(merchant_id, str(thread_id), actor_of(current_user))


@router.post("/{thread_id}/assign", response_model=Thread)
async def assign_route(
    thread_id: UUID,
    req: AssignRequest,
    merchant_id: str = _scope("assign a conversation"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Thread:
    """Give the conversation to a teammate — merchant admins only."""
    return await threads.assign(
        merchant_id, str(thread_id), req.user_id, actor_of(current_user)
    )


@router.post("/{thread_id}/handback", response_model=Thread)
async def hand_back_route(
    thread_id: UUID,
    req: ThreadAction,
    merchant_id: str = _scope("hand a conversation back"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Thread:
    return await threads.hand_back(merchant_id, str(thread_id), actor_of(current_user))


@router.post("/{thread_id}/resolve", response_model=Thread)
async def resolve_route(
    thread_id: UUID,
    req: ThreadAction,
    merchant_id: str = _scope("resolve a conversation"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Thread:
    return await threads.resolve(merchant_id, str(thread_id), actor_of(current_user))


@router.post("/{thread_id}/read", response_model=Thread)
async def read_route(
    thread_id: UUID,
    req: ThreadAction,
    merchant_id: str = _scope("mark a conversation read"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Thread:
    return await threads.mark_read(merchant_id, str(thread_id), actor_of(current_user))


@router.post("/{thread_id}/notes", response_model=TimelineRow)
async def note_route(
    thread_id: UUID,
    req: NoteRequest,
    merchant_id: str = _scope("note on a conversation"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TimelineRow:
    return await threads.add_note(
        merchant_id, str(thread_id), req.text, actor_of(current_user)
    )


@router.post("/{thread_id}/reply", response_model=ReplyRead)
async def reply_route(
    thread_id: UUID,
    req: ReplyRequest,
    merchant_id: str = _scope("reply on a conversation"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ReplyRead:
    """``text`` inside the window; an approved ``template_id`` outside it.
    Only the teammate holding the conversation replies."""
    actor = actor_of(current_user)
    if req.text is not None:
        result = await reply.reply_text(merchant_id, str(thread_id), req.text, actor)
    else:
        result = await reply.reply_template(
            merchant_id, str(thread_id), str(req.template_id), req.variables, actor
        )
    return ReplyRead(message=result.row, status=result.status, reason=result.reason)
