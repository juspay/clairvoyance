"""User management schemas for Breeze Buddy.

Unified schemas for all user account CRUD operations (admin, reseller, merchant, user).
Used by both /user-accounts and /merchants endpoints.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.breeze_buddy.auth import UserRole


class UserCreate(BaseModel):
    """User account creation request."""

    username: str = Field(
        ..., min_length=3, max_length=50, description="Unique username"
    )
    password: str = Field(..., min_length=8, description="Password")
    email: Optional[str] = Field(None, description="Email address")
    role: UserRole = Field(
        ..., description="User role: admin, reseller, merchant, user"
    )
    reseller_ids: List[str] = Field(
        default_factory=list,
        description="List of reseller IDs for access control. Used for reseller role",
    )
    merchant_identifiers: List[str] = Field(
        default_factory=list,
        description="List of merchant identifiers for access control. Required for merchant/user roles",
    )
    is_active: bool = Field(default=True, description="Account status")


class UserUpdate(BaseModel):
    """User account update request."""

    password: Optional[str] = Field(None, min_length=8, description="New password")
    email: Optional[str] = Field(None, description="Email address")
    reseller_ids: Optional[List[str]] = Field(None, description="Updated reseller IDs")
    merchant_identifiers: Optional[List[str]] = Field(
        None, description="Updated merchant identifiers"
    )
    is_active: Optional[bool] = Field(None, description="Account status")


class UserResponse(BaseModel):
    """User account response."""

    id: str
    username: str
    email: Optional[str] = None
    role: UserRole
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_identifiers: List[str] = Field(default_factory=list)
    is_active: bool = True
    owner_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserListResponse(BaseModel):
    """Response for listing user accounts."""

    users: List[UserResponse]
    total: int
    page: int = 1
    limit: int = 50
    total_pages: int = 1


class DeleteUserResponse(BaseModel):
    """Response model for delete operations."""

    success: bool
    message: str
    deleted_id: str
