"""Brute-force rate limiting for the unauthenticated credential endpoints.

Applied to ``/login``, ``/auth/s2s/token``, ``/signup`` and the password
branches of ``/auth/accounts`` and ``/auth/select-account``. Two independent
fixed-window caps count FAILED attempts only (see :func:`credential_attempt`),
so a user or service that signs in successfully is never slowed down:

- **per client IP** (shared across all the endpoints, so an attacker can't
  dodge the cap by spreading guesses across them), and
- **per username / email**, so a distributed guess against one account is
  bounded even from many source IPs.

This is defence-in-depth layered on top of the bcrypt cost and the timing
equalization (PT-16) — it caps the *number* of online guesses per window; the
bcrypt cost and the dummy-hash timing path handle per-guess cost and the
enumeration oracle.

The per-username bucket counts failures for any identifier string (existing account
or not), so a ``429`` never reveals whether an account exists — it is not an
enumeration oracle. The identifier is **SHA-256 hashed** before it becomes a
Redis key, so an attacker-supplied username of arbitrary length can neither
inflate key memory nor smuggle bytes into the key space — every key is a fixed
64-hex-char digest.

**Deployment requirement (per-IP cap).** The client IP is the entry the GCP
load balancer wrote into ``X-Forwarded-For`` (see :func:`client_ip`). If these
endpoints were ever exposed without that load balancer, a client could spoof
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
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional, Tuple

from fastapi import HTTPException, Request, status

from app.api.routers.breeze_buddy.widget_common import client_ip
from app.core.config.static import (
    AUTH_RATE_LIMIT_PER_IP,
    AUTH_RATE_LIMIT_PER_USERNAME,
    AUTH_RATE_LIMIT_WINDOW_SECONDS,
)
from app.core.logger import logger
from app.services.redis.rate_limit import check_rate_limit, refund_rate_limit

_PREFIX = "auth"


def _too_many(retry_after_seconds: int) -> HTTPException:
    # Identical response whether the IP or the username cap tripped, and whether
    # or not the account exists — nothing here distinguishes those cases.
    minutes = max(1, -(-retry_after_seconds // 60))
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            "Too many failed sign-in attempts. "
            f"Please try again in {minutes} minute{'s' if minutes != 1 else ''}."
        ),
        headers={"Retry-After": str(retry_after_seconds)},
    )


def _keys(request: Request, identifier: Optional[str]) -> List[Tuple[str, str, int]]:
    """(bucket, hashed identifier, limit) for each cap that applies.

    Both identifiers are SHA-256 hashed before they become a Redis key or a log
    field: an IP address is personal data (and check_rate_limit logs its
    identifier on Redis trouble), and an attacker-supplied username of any
    length must not inflate key memory or inject bytes into the key space. The
    digests are deterministic, so buckets are still shared across pods.
    """
    ip_key = hashlib.sha256(client_ip(request).encode("utf-8")).hexdigest()
    keys = [("credential_ip", ip_key, AUTH_RATE_LIMIT_PER_IP)]
    username = (identifier or "").strip().lower()
    if username:
        user_key = hashlib.sha256(username.encode("utf-8")).hexdigest()
        keys.append(("credential_user", user_key, AUTH_RATE_LIMIT_PER_USERNAME))
    return keys


@asynccontextmanager
async def credential_attempt(
    request: Request, identifier: Optional[str]
) -> AsyncIterator[None]:
    """Count the wrapped credential check against the caps if it FAILS.

    Raises ``429`` (with ``Retry-After``) before the check runs when the
    caller's IP or the targeted username/email has used up its failures for the
    window. Otherwise a slot is taken up front — so a burst of parallel guesses
    cannot all slip under the cap before any of them fails — and kept only if
    the wrapped block raises a 401 (wrong credentials). Success and every other
    outcome (409, 403, a server error) hand it back.

    Args:
        request: The inbound request, used to derive the client IP (via
            :func:`client_ip`).
        identifier: Username, email or account id the request targets. May be
            ``None``/empty (e.g. an SSO-only call); only the per-IP cap then
            applies.
    """
    taken: List[Tuple[str, str]] = []
    for bucket, key, limit in _keys(request, identifier):
        decision = await check_rate_limit(
            bucket=bucket,
            identifier=key,
            limit=limit,
            window_seconds=AUTH_RATE_LIMIT_WINDOW_SECONDS,
            prefix=_PREFIX,
        )
        if not decision.allowed:
            # A refused request is not an attempt: release what it took.
            for b, k in taken + [(bucket, key)]:
                await refund_rate_limit(
                    bucket=b,
                    identifier=k,
                    window_seconds=AUTH_RATE_LIMIT_WINDOW_SECONDS,
                    prefix=_PREFIX,
                )
            # Log a bounded prefix of the hashed identifier: stable, so ops can
            # correlate repeated hits on one bucket, and no username, email or
            # IP reaches the logs.
            logger.warning(
                f"auth rate limit hit ({bucket} {decision.limit} failures per "
                f"{AUTH_RATE_LIMIT_WINDOW_SECONDS}s) for hash {key[:16]}"
            )
            raise _too_many(decision.retry_after_seconds)
        taken.append((bucket, key))

    try:
        yield
    except HTTPException as exc:
        # Only a 401 is a failed guess. Anything else — a taken username on
        # /signup (409), a non-admin with the right password on /auth/s2s/token
        # (403) — is not, and must not burn the caller's attempts.
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            await _release(taken)
        raise
    except Exception:
        # Our own failure (a database outage, a bug) is not a guess either.
        await _release(taken)
        raise
    await _release(taken)


async def _release(taken: List[Tuple[str, str]]) -> None:
    for bucket, key in taken:
        await refund_rate_limit(
            bucket=bucket,
            identifier=key,
            window_seconds=AUTH_RATE_LIMIT_WINDOW_SECONDS,
            prefix=_PREFIX,
        )
