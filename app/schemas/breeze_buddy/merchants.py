"""Response schemas for merchant endpoints."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class MerchantCreate(BaseModel):
    """Create a new merchant entity (business entity).

    - Admin: can set reseller_id to assign merchant to a reseller (or leave null)
    - Reseller: reseller_id is auto-set to their own user ID
    """

    merchant_id: str = Field(
        ...,
        min_length=3,
        max_length=100,
        description="Unique business identifier (alphanumeric, underscore, hyphen only)",
    )
    name: Optional[str] = Field(
        None, max_length=255, description="Optional display name"
    )
    description: Optional[str] = Field(None, description="Optional description")
    is_active: Optional[bool] = Field(True, description="Active status (default: true)")
    reseller_id: Optional[str] = Field(
        None,
        description="Reseller user ID who owns this merchant. "
        "Admin-only field; auto-set for resellers.",
    )


class MerchantUpdate(BaseModel):
    """Update a merchant entity. merchant_id cannot be changed."""

    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    reseller_id: Optional[str] = Field(
        None, description="Update reseller assignment (admin only)"
    )


class MerchantResponse(BaseModel):
    """Merchant entity response."""

    merchant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    reseller_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MerchantListResponse(BaseModel):
    """Response for listing merchant entities."""

    merchants: List[MerchantResponse]
    total: int
    page: int = 1
    limit: int = 50
    total_pages: int = 1
