"""
Pydantic schemas for Blueprint agent sessions.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class BlueprintMode(str, Enum):
    CREATE = "create"
    EDIT = "edit"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class BlueprintSessionModel(BaseModel):
    id: str
    user_id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    mode: str
    template_id: Optional[str] = None
    langgraph_thread_id: str
    current_step: Optional[str] = None
    status: str = "active"
    result_template_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class CreateSessionRequest(BaseModel):
    mode: BlueprintMode = BlueprintMode.CREATE
    template_id: Optional[str] = None  # Required for edit mode
    reseller_id: Optional[str] = None
    """Optional. Non-admin users have it auto-derived from their JWT
    (``UserInfo.reseller_ids``). Admin users may pass any reseller_id;
    non-admins are restricted to their authorized list.
    """
    merchant_id: Optional[str] = None
    """Optional. Same auth-derivation rule as ``reseller_id``."""


class CreateSessionResponse(BaseModel):
    session_id: str
    langgraph_thread_id: str
    mode: str
    status: str


class SessionListResponse(BaseModel):
    sessions: list[BlueprintSessionModel]
    total: int
