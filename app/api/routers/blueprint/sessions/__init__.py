"""
Blueprint session management endpoints.

Endpoints:
- POST   /sessions              - Create a new blueprint session
- GET    /sessions              - List user's sessions
- GET    /sessions/{session_id} - Get session detail
- DELETE /sessions/{session_id} - Delete a session
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.routers.blueprint.rbac import get_blueprint_user
from app.schemas import UserInfo
from app.schemas.blueprint.session import (
    BlueprintSessionModel,
    CreateSessionRequest,
    CreateSessionResponse,
    SessionListResponse,
)

from .handlers import (
    create_session_handler,
    delete_session_handler,
    get_session_handler,
    list_sessions_handler,
)

router = APIRouter()


@router.post(
    "/sessions",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    request: CreateSessionRequest,
    current_user: UserInfo = Depends(get_blueprint_user),
):
    """
    Create a new blueprint session.

    Initializes a new conversational session for creating or editing a template
    via the Blueprint agent.

    Request Body:
        - mode: "create" or "edit"
        - template_id: Required when mode is "edit"
        - reseller_id: Reseller ID for the session
        - merchant_id: Optional merchant ID

    Returns:
        CreateSessionResponse with session_id and langgraph_thread_id
    """
    return await create_session_handler(request, current_user)


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by session status"
    ),
    current_user: UserInfo = Depends(get_blueprint_user),
):
    """
    List the current user's blueprint sessions.

    Query Parameters:
        - status: Optional filter by session status (active, completed, abandoned)

    Returns:
        SessionListResponse with list of sessions and total count
    """
    return await list_sessions_handler(current_user, status_filter)


@router.get("/sessions/{session_id}", response_model=BlueprintSessionModel)
async def get_session(
    session_id: str,
    current_user: UserInfo = Depends(get_blueprint_user),
):
    """
    Get a blueprint session by ID.

    Path Parameters:
        - session_id: Session UUID

    Returns:
        BlueprintSessionModel with full session details
    """
    return await get_session_handler(session_id, current_user)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def delete_session(
    session_id: str,
    current_user: UserInfo = Depends(get_blueprint_user),
):
    """
    Delete a blueprint session.

    Path Parameters:
        - session_id: Session UUID

    Returns:
        Success message
    """
    return await delete_session_handler(session_id, current_user)
