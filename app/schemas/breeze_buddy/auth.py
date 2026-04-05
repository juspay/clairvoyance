"""Authentication and authorization schemas for Breeze Buddy."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


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
    password: str


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
    password: str
    token_lifetime_days: int = Field(
        default=365, ge=1, le=365000, description="Token lifetime in days (1-365000)"
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
