"""Pydantic models for token_registry rows (migrations 086, 087).

One row per long-lived RBAC token: the S2S token from ``POST /auth/s2s/token``,
the per-merchant token from ``POST /merchant``, and any older long-lived token
a verifier has seen since (``source = "backfilled"``). The row holds the
token's sha256 hash, never the token. See
``app/api/security/breeze_buddy/rbac_token.py`` (``create_registered_token``,
``authenticate``).
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Which mint door issued the token: "admin" (POST /auth/s2s/token) or
# "merchant" (POST /merchant with issue_token).
TokenKind = Literal["admin", "merchant"]

# How the row got here: written at mint, or registered later from use.
TokenSource = Literal["minted", "backfilled"]


class TokenRegistryEntry(BaseModel):
    """One registered token. ``id`` is the token's ``jti`` claim."""

    id: str
    token_hash: str
    kind: TokenKind
    source: TokenSource
    subject: str
    username: str
    role: str
    reseller_ids: List[str]
    merchant_ids: List[str]
    created_by: Optional[str] = None
    issued_at: datetime
    expires_at: datetime
    created_at: datetime
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None
    revoke_reason: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    last_seen_from: Optional[str] = None
    last_route: Optional[str] = None


class TokenListResponse(BaseModel):
    """Body of ``GET /auth/tokens``."""

    tokens: List[TokenRegistryEntry]
    limit: int
    offset: int


class RevokeTokenRequest(BaseModel):
    """Body of ``POST /auth/tokens/{token_id}/revoke``."""

    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class RevokeTokenResponse(BaseModel):
    """Body of the revoke answer: the row as revoked, and how many
    ``merchants.s2s_token`` values held this token and were cleared."""

    token: TokenRegistryEntry
    merchant_tokens_cleared: int
