"""User management schemas for Breeze Buddy.

Unified schemas for all user account CRUD operations (admin, reseller, merchant, user).
Used by both /user-accounts and /merchants endpoints.
"""

import re
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.security.password import (
    MIN_PASSWORD_LENGTH,
    validate_password_strength,
)
from app.schemas.breeze_buddy.auth import UserRole


class UserCreate(BaseModel):
    """User account creation request."""

    id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Unique human-readable ID (e.g. 'reseller_acme', 'merchant_redbus'). No spaces allowed.",
    )
    username: str = Field(
        ..., min_length=3, max_length=50, description="Unique username"
    )
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, description="Password")
    email: Optional[str] = Field(None, description="Email address")
    role: UserRole = Field(
        ..., description="User role: admin, reseller, merchant, user"
    )
    reseller_ids: List[str] = Field(
        default_factory=list,
        description="List of reseller IDs for access control. Used for reseller role",
    )
    merchant_ids: List[str] = Field(
        default_factory=list,
        description="List of merchant identifiers for access control. Required for merchant/user roles",
    )
    is_active: bool = Field(default=True, description="Account status")

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if re.search(r"\s", v):
            raise ValueError("ID must not contain spaces")
        return v

    @model_validator(mode="after")
    def _validate_password_policy(self) -> "UserCreate":
        validate_password_strength(
            self.password,
            disallowed_substrings=[
                self.username,
                self.id,
                (self.email or "").split("@")[0],
            ],
        )
        return self


class UserUpdate(BaseModel):
    """User account update request."""

    password: Optional[str] = Field(
        None, min_length=MIN_PASSWORD_LENGTH, description="New password"
    )
    email: Optional[str] = Field(None, description="Email address")
    reseller_ids: Optional[List[str]] = Field(None, description="Updated reseller IDs")
    merchant_ids: Optional[List[str]] = Field(
        None, description="Updated merchant identifiers"
    )
    is_active: Optional[bool] = Field(None, description="Account status")

    @model_validator(mode="after")
    def _validate_password_policy(self) -> "UserUpdate":
        if self.password is not None:
            validate_password_strength(
                self.password,
                disallowed_substrings=[(self.email or "").split("@")[0]],
            )
        return self


class UserResponse(BaseModel):
    """User account response."""

    id: str
    username: str
    email: Optional[str] = None
    role: UserRole
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_ids: List[str] = Field(default_factory=list)
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
