"""Authentication and authorization schemas for Breeze Buddy."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

# PT-21. Every path that mints a long-lived S2S token must bound it by this,
# not by its own literal — there is more than one such path (POST /auth/s2s/token
# and POST /merchant with issue_token=true), and they both hand the value
# straight to rbac_token_manager.create_access_token_with_rbac. A per-schema
# literal is how the two drifted to 365 and 365000 in the first place.
MAX_S2S_TOKEN_LIFETIME_DAYS = 365


class TokenData(BaseModel):
    """Token data model for JWT payload (legacy)"""

    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)


class UserRole(str, Enum):
    """User role enum for RBAC"""

    ADMIN = "admin"
    RESELLER = "reseller"
    MERCHANT = "merchant"
    USER = "user"  # Renamed from SHOP


class Permission(str, Enum):
    """Permission enum for granular access control"""

    # Read permissions
    READ_ALL = "read:all"
    READ_OWN_DATA = "read:own_data"
    READ_ANALYTICS = "read:analytics"
    READ_ASSIGNED_MERCHANTS = "read:assigned_merchants"

    # Write permissions
    WRITE_ALL = "write:all"
    WRITE_OWN_DATA = "write:own_data"
    WRITE_ASSIGNED_MERCHANTS = "write:assigned_merchants"

    # Delete permissions
    DELETE_ALL = "delete:all"
    DELETE_OWN_DATA = "delete:own_data"

    # Analytics permissions
    ANALYTICS_ALL = "analytics:all"
    ANALYTICS_OWN = "analytics:own"
    ANALYTICS_ASSIGNED_MERCHANTS = "analytics:assigned_merchants"

    # Configuration permissions
    CONFIGURATIONS_ALL = "configurations:all"
    CONFIGURATIONS_READ = "configurations:read"
    CONFIGURATIONS_ASSIGNED_MERCHANTS = "configurations:assigned_merchants"

    # Merchant management
    MERCHANTS_ALL = "merchants:all"


class LoginRequest(BaseModel):
    """Login request model"""

    username: str
    # min_length=1: bcrypt's verifier rejects an empty plaintext with
    # ValueError, which would surface as a 500 instead of the generic 401.
    # Fail at the schema boundary (422) so it never reaches verify_password.
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    """Login response model (deprecated - use TokenResponse)"""

    success: bool
    detail: Optional[str] = None


class TokenResponse(BaseModel):
    """JWT token response model"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds


class S2STokenRequest(BaseModel):
    """S2S token generation request model"""

    username: str
    # See LoginRequest.password — empty plaintext must 422, never reach bcrypt.
    password: str = Field(..., min_length=1)
    token_lifetime_days: int = Field(
        default=MAX_S2S_TOKEN_LIFETIME_DAYS,
        ge=1,
        le=MAX_S2S_TOKEN_LIFETIME_DAYS,
        description=f"Token lifetime in days (1-{MAX_S2S_TOKEN_LIFETIME_DAYS})",
    )
    reseller_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Restrict token scope to these reseller IDs. "
            "Must be a subset of the admin's own reseller access. "
            "Omit to inherit all admin scopes."
        ),
    )
    merchant_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Restrict token scope to these merchant IDs. "
            "Must be a subset of the admin's own merchant access. "
            "Omit to inherit all admin scopes."
        ),
    )


class S2STokenResponse(BaseModel):
    """S2S token generation response model"""

    success: bool
    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds
    expires_at: str  # ISO datetime
    note: str


class UserInfo(BaseModel):
    """User information extracted from JWT token"""

    id: str
    username: str
    role: UserRole
    email: Optional[str] = None
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_ids: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    owner_id: Optional[str] = None  # UUID of user who created this account


class UserCreate(BaseModel):
    """User creation request model"""

    username: str
    password: str
    role: UserRole
    email: Optional[str] = None
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_ids: List[str] = Field(default_factory=list)
    is_active: bool = True


class UserUpdate(BaseModel):
    """User update request model"""

    password: Optional[str] = None
    role: Optional[UserRole] = None
    email: Optional[str] = None
    reseller_ids: Optional[List[str]] = None
    merchant_ids: Optional[List[str]] = None
    is_active: Optional[bool] = None


class UserInDB(BaseModel):
    """User model as stored in database"""

    id: str
    username: str
    password_hash: str
    role: UserRole
    email: Optional[str] = None
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_ids: List[str] = Field(default_factory=list)
    is_active: bool = True
    owner_id: Optional[str] = None  # UUID of user who created this account
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class User(BaseModel):
    """User model for API responses (without password_hash)"""

    id: str
    username: str
    role: UserRole
    email: Optional[str] = None
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_ids: List[str] = Field(default_factory=list)
    is_active: bool = True
    owner_id: Optional[str] = None  # UUID of user who created this account
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LaunchTokenRequest(BaseModel):
    """Request to mint a short-lived merchant-scoped Loom launch token.

    Used by trusted callers (e.g. Nautilus/Buddy Assist) to hand a merchant
    a signed-in Loom session without provisioning a real ``users`` row.
    """

    reseller_id: str = Field(..., description="Reseller that owns the merchant")
    merchant_id: str = Field(..., description="Merchant to mint a session for")
    source: str = Field(
        ...,
        description=(
            "Requesting product (e.g. 'nautilus'). Stamped into the token's "
            "signed 'src' claim and gated by Loom on sign-in. Must be a known "
            "launch source."
        ),
    )
    redirect: Optional[str] = Field(
        None,
        description=(
            "Relative path Loom should land on after sign-in. Must start with a "
            "single '/' and contain only [A-Za-z0-9._~/-] (relative, not "
            "protocol-relative). Defaults to /home."
        ),
    )


class LaunchTokenResponse(BaseModel):
    """Response containing a 1-hour merchant-scoped session JWT for Loom."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds
    launch_url: str


class AuthTokenData(BaseModel):
    """Enhanced token data model for JWT payload with RBAC"""

    sub: str  # user_id
    username: str
    role: UserRole
    email: Optional[str] = None
    reseller_ids: List[str] = Field(default_factory=list)
    merchant_ids: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    iat: Optional[int] = None  # issued at
    exp: Optional[int] = None  # expiration
