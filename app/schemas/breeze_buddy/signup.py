"""
Self-service merchant signup schemas.

These schemas are used by the public signup endpoints:
- POST /signup          — username/password registration
- POST /auth/google     — Google SSO login (existing user)
- POST /signup/google   — Google SSO registration (new merchant)

Security notes:
- `role` is intentionally absent — the backend always assigns UserRole.MERCHANT.
- `reseller_id` is always hardcoded to "breeze" server-side; the client is
  never trusted to supply it.
- No admin-only fields (reseller_ids, merchant_ids wildcards) are exposed.
"""

import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class MerchantSignupRequest(BaseModel):
    """
    Self-service merchant registration via username + password.

    Creates both a merchant entity (in `merchants` table) AND a user
    login account (in `users` table) scoped to that merchant in one
    atomic operation.
    """

    # ── Merchant entity fields ────────────────────────────────────────────
    merchant_id: str = Field(
        ...,
        min_length=3,
        max_length=100,
        description="Unique merchant identifier (letters, numbers, hyphens, underscores, dots).",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Business display name.",
    )
    description: Optional[str] = Field(
        None,
        description="Optional business description.",
    )

    # ── Login account fields ──────────────────────────────────────────────
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="Login username (must be unique across the platform).",
    )
    password: str = Field(
        ...,
        min_length=8,
        description="Password (minimum 8 characters).",
    )
    email: Optional[str] = Field(
        None,
        description="Optional email address.",
    )

    @field_validator("merchant_id")
    @classmethod
    def validate_merchant_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_.\-]+$", v):
            raise ValueError(
                "merchant_id may only contain letters, numbers, hyphens, underscores, and dots"
            )
        return v

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if re.search(r"\s", v):
            raise ValueError("username must not contain spaces")
        return v


class GoogleAuthRequest(BaseModel):
    """
    Google SSO login for an *existing* user.

    The frontend passes the `credential` (id_token) returned by the
    Google Identity Services `accounts.id.initialize` callback.
    The backend verifies the token with Google's public keys, extracts
    the email, and looks up a matching user account.

    If no account is found the backend returns HTTP 404 with
    `detail: "no_account"` so the frontend can redirect to the signup
    step.
    """

    id_token: str = Field(
        ...,
        description="Google id_token (JWT) returned by the GSI SDK credential callback.",
    )


class GoogleMerchantSignupRequest(BaseModel):
    """
    Google SSO self-service merchant registration (step 2).

    Called after `POST /auth/google` returns 404 (no existing account).
    The frontend re-sends the same `id_token` together with the merchant
    details the user filled in.

    The backend re-verifies the token, extracts the Google email as the
    account email, then creates the merchant entity + user login account.
    """

    id_token: str = Field(
        ...,
        description="The same Google id_token from the initial GSI callback.",
    )

    # ── Merchant entity fields ────────────────────────────────────────────
    merchant_id: str = Field(
        ...,
        min_length=3,
        max_length=100,
        description="Unique merchant identifier.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Business display name.",
    )
    description: Optional[str] = Field(
        None,
        description="Optional business description.",
    )

    @field_validator("merchant_id")
    @classmethod
    def validate_merchant_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_.\-]+$", v):
            raise ValueError(
                "merchant_id may only contain letters, numbers, hyphens, underscores, and dots"
            )
        return v


class ListAccountsRequest(BaseModel):
    """Request body for listing accounts by email."""

    id_token: Optional[str] = Field(
        None, description="Google id_token (for SSO flows)."
    )
    email: Optional[str] = Field(
        None, description="Email address (for password flows)."
    )


class AccountSummary(BaseModel):
    """
    Lightweight summary of a single user account for the account-picker UI.
    No sensitive data (no password_hash).
    """

    id: str
    username: str
    role: str
    email: Optional[str] = None
    merchant_ids: List[str] = Field(default_factory=list)
    reseller_ids: List[str] = Field(default_factory=list)
    is_active: bool = True


class AccountsResponse(BaseModel):
    """Response from the account-picker endpoint."""

    accounts: List[AccountSummary]


class SelectAccountRequest(BaseModel):
    """Select a specific account to log in as."""

    account_id: str = Field(..., description="The `id` of the account to log in as.")
    id_token: Optional[str] = Field(
        None, description="Google id_token (for SSO flows)."
    )
    password: Optional[str] = Field(None, description="Password (for password flows).")


class SwitchAccountRequest(BaseModel):
    """Switch to a sibling account while already authenticated."""

    account_id: str = Field(..., description="The `id` of the account to switch to.")
