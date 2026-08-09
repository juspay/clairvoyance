"""Response schemas for merchant endpoints."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.breeze_buddy.auth import MAX_S2S_TOKEN_LIFETIME_DAYS


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
    issue_token: bool = Field(
        False,
        description="If true, mint a per-merchant S2S token, store it on the "
        "merchant row, and return it (once) in the response. Used e.g. as the "
        "webhook HMAC secret. Requires a reseller_id.",
    )
    # PT-21. This mints a real RBAC JWT through the same helper as
    # POST /auth/s2s/token (merchants/handlers.py: create_access_token_with_rbac),
    # so it takes the same cap. It previously defaulted to 3650 days and allowed
    # 365000 — and unlike /auth/s2s/token, which is admin-only, this endpoint is
    # reachable by resellers too.
    token_lifetime_days: int = Field(
        default=MAX_S2S_TOKEN_LIFETIME_DAYS,
        ge=1,
        le=MAX_S2S_TOKEN_LIFETIME_DAYS,
        description=(
            "Lifetime of the issued token in days (only when issue_token). "
            f"Capped at {MAX_S2S_TOKEN_LIFETIME_DAYS} (PT-21)."
        ),
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
    """Merchant entity response.

    ``token`` / ``token_expires_at`` are populated only on create when
    ``issue_token`` was requested; they are null on list/get/update.
    """

    merchant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    reseller_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    token: Optional[str] = Field(
        None,
        description="Per-merchant S2S token, returned once when issue_token=true. "
        "Store it now — it is not retrievable later.",
    )
    token_expires_at: Optional[str] = Field(
        None, description="ISO timestamp when the issued token expires"
    )


class MerchantListResponse(BaseModel):
    """Response for listing merchant entities."""

    merchants: List[MerchantResponse]
    total: int
    page: int = 1
    limit: int = 50
    total_pages: int = 1
