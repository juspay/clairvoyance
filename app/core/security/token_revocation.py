"""
Server-side JWT revocation denylist (Redis-backed).

Keyed by a SHA-256 hash of the token's canonical spelling, so it retroactively
covers every already-issued token without needing a `jti` claim or a re-mint.
Canonical because PyJWT decodes the signature segment leniently: `=` padding,
stray non-base64 characters and the unused low bits of its last character are
all ignored, so one token has many spellings that verify — and a hash of the raw
text would let a logged-out token back in as `token + "="`. Entries are
stored with a TTL equal to the token's remaining lifetime, so the denylist
self-cleans as tokens expire.

Fail-open on Redis errors: a Redis outage must not lock out every user (tokens
still expire naturally, and this denylist is one layer among several). The
liveness recheck and short-TTL guidance in PT-22 complement it.
"""

import hashlib
import math
import time

from jwt.utils import base64url_decode, base64url_encode

from app.core.logger import logger
from app.services.redis.client import get_redis_service

_REVOKED_PREFIX = "jwt:revoked:"


def _canonical(token: str) -> str:
    # header.payload is signed byte-for-byte, so only the signature can vary;
    # split it the way PyJWT does (last dot) and re-encode it.
    signing_input, _, signature = token.rpartition(".")
    try:
        return (
            f"{signing_input}.{base64url_encode(base64url_decode(signature)).decode()}"
        )
    except Exception:
        return token


def _denylist_key(token: str) -> str:
    canonical = _canonical(token)
    return _REVOKED_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def revoke_token(token: str, exp: int) -> bool:
    """Add a token to the denylist until its own expiry. Returns success."""
    # Round up rather than truncate: int() turns a token with 0.4s of life left
    # into ttl == 0, which takes the "already expired" path below and reports
    # success without ever writing the denylist entry — while the token still
    # authenticates for the remainder of that second.
    ttl = math.ceil(exp - time.time())
    if ttl <= 0:
        return True  # genuinely expired — nothing to revoke
    try:
        redis = await get_redis_service()
        return await redis.setex(_denylist_key(token), "1", ttl_seconds=ttl)
    except Exception as e:
        logger.error(f"Failed to revoke JWT: {e}")
        return False


async def is_token_revoked(token: str) -> bool:
    """True if the token has been revoked. Fails open (False) on Redis error."""
    try:
        redis = await get_redis_service()
        return await redis.exists(_denylist_key(token))
    except Exception as e:
        logger.error(f"Token revocation check failed (failing open): {e}")
        return False
