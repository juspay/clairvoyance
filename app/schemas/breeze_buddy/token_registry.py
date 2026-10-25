"""Pydantic models for token_registry rows (migration 086).

One row per long-lived RBAC token: the S2S token from ``POST /auth/s2s/token``
and the per-merchant token from ``POST /merchant``. The row holds the token's
sha256 hash, never the token. See
``app/api/security/breeze_buddy/rbac_token.py`` (``create_registered_token``).
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel

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
