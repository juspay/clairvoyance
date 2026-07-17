"""Request/response schemas for reseller (umbrella) endpoints."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ResellerCreate(BaseModel):
    """Create a new reseller (umbrella) entity.

    Creating a reseller entity does NOT create a login. A login for the
    umbrella is an ordinary users row (role='reseller') with the same id,
    created through the users API.
    """

    id: str = Field(
        ...,
        min_length=2,
        max_length=255,
        pattern=r"^\S+$",
        description="Umbrella slug (no whitespace). Doubles as the login id "
        "if a reseller login is created for this umbrella.",
    )
    name: Optional[str] = Field(
        None, max_length=255, description="Optional display name"
    )
    description: Optional[str] = Field(None, description="Optional description")
    is_active: bool = Field(True, description="Active status (default: true)")


class ResellerUpdate(BaseModel):
    """Update a reseller entity (id cannot be changed)."""

    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ResellerResponse(BaseModel):
    """Reseller entity as returned by the API."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    workspace_count: int = 0
    member_count: int = Field(
        0, description="Users holding an access grant on this umbrella"
    )
    has_login: bool = Field(
        False, description="Whether a role='reseller' users row with this id exists"
    )
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ResellerListResponse(BaseModel):
    """Paginated reseller list."""

    resellers: List[ResellerResponse]
    total: int
    page: int
    limit: int
    total_pages: int


class UmbrellaGrant(BaseModel):
    """One user_reseller_access row, resolved for display."""

    reseller_id: str
    reseller_name: Optional[str] = None
    all_workspaces: bool = False


class WorkspaceAccess(BaseModel):
    """One workspace a user can reach, with how they reach it."""

    merchant_id: str
    name: Optional[str] = None
    source: str = Field(
        ...,
        description="'explicit' (direct membership row) or 'inherited' "
        "(via an all-workspaces umbrella grant)",
    )
    via_reseller: Optional[str] = Field(
        None, description="Umbrella the access is inherited through (inherited only)"
    )


class UserAccessResponse(BaseModel):
    """Effective access for one user, from the normalized grant tables."""

    user_id: str
    role: str
    unrestricted: bool = Field(
        False, description="True for admin accounts (no grant rows needed)"
    )
    umbrella_grants: List[UmbrellaGrant] = Field(default_factory=list)
    workspaces: List[WorkspaceAccess] = Field(default_factory=list)
