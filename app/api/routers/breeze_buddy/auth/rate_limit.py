"""Brute-force rate limiting for the unauthenticated credential endpoints.

Applied to ``/login``, ``/auth/s2s/token``, ``/signup`` and ``/auth/accounts``.
Two independent fixed-window caps run before the password is ever checked:

- **per client IP** (shared across all four endpoints, so an attacker can't
  dodge the cap by spreading guesses across endpoints), and
- **per username / email**, so a distributed guess against one account is
  bounded even from many source IPs.

This is defence-in-depth layered on top of the bcrypt cost and the timing
equalization (PT-16) — it caps the *number* of online guesses per window; the
bcrypt cost and the dummy-hash timing path handle per-guess cost and the
enumeration oracle.

The per-username bucket increments for any identifier string (existing account
or not), so a ``429`` never reveals whether an account exists — it is not an
enumeration oracle. The identifier is **SHA-256 hashed** before it becomes a
Redis key, so an attacker-supplied username of arbitrary length can neither
inflate key memory nor smuggle bytes into the key space — every key is a fixed
64-hex-char digest.

**Deployment requirement (per-IP cap).** The client IP is the last-hop
``X-Forwarded-For`` value (see :func:`client_ip`). That is correct only behind a
trusted proxy that *overwrites* inbound XFF (Cloud Run / the ingress nginx do);
if these endpoints were ever exposed without such a proxy, a client could spoof
``X-Forwarded-For`` to burn a victim's per-IP bucket. The per-username cap is
unaffected by XFF and keeps a distributed guess bounded regardless.

**Fail-open on Redis trouble.** ``check_rate_limit`` is called with the default
``fail_closed=False``: if Redis is down or unconfigured the request is allowed.
Unlike the anonymous, LLM-cost-amplifying widget endpoints (which fail closed),
locking every operator — including admins trying to fix the incident — out of
login during a Redis blip is worse than briefly losing this defence-in-depth
layer; the bcrypt cost still bounds throughput. Flip the caps to tune, or raise
them in code if a deployment wants a fail-closed posture here.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import HTTPException, Request, status

from app.api.routers.breeze_buddy.widget_common import client_ip
from app.core.config.static import (
    AUTH_RATE_LIMIT_PER_IP_PER_HOUR,
    AUTH_RATE_LIMIT_PER_USERNAME_PER_HOUR,
)
from app.core.logger import logger
from app.services.redis.rate_limit import check_rate_limit

_WINDOW_SECONDS = 3600
_PREFIX = "auth"


def _too_many(retry_after_seconds: int) -> HTTPException:
    # Identical response whether the IP or the username cap tripped, and whether
    # or not the account exists — nothing here distinguishes those cases.
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many authentication attempts. Please try again later.",
        headers={"Retry-After": str(retry_after_seconds)},
    )


async def enforce_credential_rate_limit(
    request: Request, identifier: Optional[str]
) -> None:
    """Raise ``429`` when the caller's IP or the target identifier is over cap.

    Args:
        request: The inbound request, used to derive the client IP (last-hop
            ``X-Forwarded-For`` via :func:`client_ip`).
        identifier: Username or email the request targets. May be ``None``/empty
            (e.g. an SSO-only account-list call); the per-username cap is then
            skipped and only the per-IP cap applies.
    """
    ip = client_ip(request)
    ip_decision = await check_rate_limit(
        bucket="credential_ip",
        identifier=ip,
        limit=AUTH_RATE_LIMIT_PER_IP_PER_HOUR,
        window_seconds=_WINDOW_SECONDS,
        prefix=_PREFIX,
    )
    if not ip_decision.allowed:
        logger.warning(
            f"auth rate limit hit (per-IP {ip_decision.count}/{ip_decision.limit}) "
            f"for {ip!r}"
        )
        raise _too_many(ip_decision.retry_after_seconds)

    username = (identifier or "").strip().lower()
    if not username:
        return

    # Hash the attacker-supplied identifier so the Redis key is a fixed-length
    # digest — an arbitrarily long username can't inflate key memory or inject
    # bytes into the key space. Hashing is deterministic, so the per-username
    # bucket is still shared across pods.
    username_key = hashlib.sha256(username.encode("utf-8")).hexdigest()

    user_decision = await check_rate_limit(
        bucket="credential_user",
        identifier=username_key,
        limit=AUTH_RATE_LIMIT_PER_USERNAME_PER_HOUR,
        window_seconds=_WINDOW_SECONDS,
        prefix=_PREFIX,
    )
    if not user_decision.allowed:
        # Log a bounded prefix of the raw username (not the hash) for ops
        # triage without letting a huge identifier bloat the log line.
        logger.warning(
            f"auth rate limit hit (per-username "
            f"{user_decision.count}/{user_decision.limit}) for {username[:64]!r}"
        )
        raise _too_many(user_decision.retry_after_seconds)
