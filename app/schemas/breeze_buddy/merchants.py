"""Response schemas for merchant endpoints."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


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
    token_lifetime_days: int = Field(
        default=3650,
        ge=1,
        le=365000,
        description="Lifetime of the issued token in days (only when issue_token).",
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


# --------------------------------------------------------------------------
# Per-customer call limits (ADR 0025 stage 1) — the merchant's rule, enforced
# at the dial by the dispatch worker (services/call_limiter.py).
# --------------------------------------------------------------------------

# A rolling window longer than a week is not a call-frequency rule any more.
CALL_LIMIT_MAX_WINDOW_HOURS = 168
# Stage 1 takes exactly one rule; stage 2 lifts this for "a day AND a week".
CALL_LIMIT_MAX_RULES = 1


class CallLimit(BaseModel):
    """At most ``max_calls`` dials to one customer in any rolling
    ``window_hours`` — rolling, not calendar: a merchant-chosen window has no
    midnight to reset at.

    Strict ints: ``"3"`` and ``true`` are refused rather than coerced — a
    limit read wrong in the permission-adjacent direction calls someone the
    merchant said to stop calling.
    """

    model_config = ConfigDict(extra="forbid")

    max_calls: StrictInt = Field(..., ge=1, description="Dials allowed in the window")
    window_hours: StrictInt = Field(
        ...,
        ge=1,
        le=CALL_LIMIT_MAX_WINDOW_HOURS,
        description="Rolling window length in hours (1-168)",
    )


class CallLimitsUpdate(BaseModel):
    """``PUT /merchant/{merchant_id}/call-limits`` body.

    ``call_limits`` is REQUIRED so clearing is explicit: ``null`` or ``[]``
    removes the rule (stored as NULL — one form for "no rule").
    """

    model_config = ConfigDict(extra="forbid")

    call_limits: Optional[List[CallLimit]] = Field(
        ...,
        max_length=CALL_LIMIT_MAX_RULES,
        description="The merchant's per-customer call rules; null or [] for none",
    )

    @field_validator("call_limits")
    @classmethod
    def _empty_is_none(cls, value: Optional[List[CallLimit]]):
        return value or None


class CallLimitsResponse(BaseModel):
    """The merchant's per-customer call rules (``null`` = no rule)."""

    merchant_id: str
    call_limits: Optional[List[CallLimit]] = None
