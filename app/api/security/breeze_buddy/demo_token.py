"""Demo-session JWT — mint + verify for the public chat demo.

Why a separate token type instead of reusing the standard RBAC JWT:

- The public demo endpoints have **no upstream user**. Visitors hit
  ``POST /chat/demo/session`` (slug in the JSON body) anonymously and
  need a credential for the subsequent ``/message`` SSE call.
- Reusing the RBAC JWT would mean handing out a token with non-empty
  ``reseller_ids`` / ``merchant_ids`` to anonymous visitors, which would
  authenticate them on *every* breeze_buddy endpoint that accepts those
  scopes. That's a wide blast radius for a token we mint freely.

The demo token therefore:

1. Carries a ``demo: true`` claim — the standard RBAC verifier
   (``rbac_token.py``) doesn't read this, so a demo token will never
   pass standard auth. The demo dependency below is the only verifier
   that will accept it.
2. Pins ``demo_session_id`` to the chat session created at mint time.
   The dependency rejects any path-param ``session_id`` that doesn't
   match — so a leaked token can't be steered to other sessions.
3. Carries the per-session ``demo_message_cap`` so the message handler
   enforces it without an extra DB read.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Any, Dict

import jwt as pyjwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config.dynamic import DEMO_TOKEN_TTL_MINUTES
from app.core.logger import logger
from app.core.security.jwt import jwt_manager

# Reuse the existing HTTPBearer scheme so OpenAPI sees one consistent
# auth header across the API. ``auto_error=True`` returns 403 when the
# header is missing — same behaviour as the RBAC dependency.
_demo_bearer = HTTPBearer(auto_error=True, scheme_name="DemoBearer")


class DemoSessionContext:
    """Resolved demo-token claims, returned by :func:`require_demo_session`.

    Mirrors the read-only shape of ``UserInfo`` callers expect from the
    standard RBAC dep — but the demo handler treats it specifically, so
    no abstract base is needed.
    """

    __slots__ = ("session_id", "message_cap", "subject")

    def __init__(self, *, session_id: str, message_cap: int, subject: str) -> None:
        self.session_id = session_id
        self.message_cap = message_cap
        self.subject = subject


async def mint_demo_token(*, session_id: str, message_cap: int) -> str:
    """Issue a demo JWT bound to ``session_id`` for ``DEMO_TOKEN_TTL_MINUTES``.

    ``sub`` is a fresh anonymous identifier (``demo-<short-uuid>``) — useful
    in logs but never used for authorization. The actual gate is the
    ``demo_session_id`` claim verified below. Async because the TTL is a
    Redis/DevCycle-backed dynamic knob.
    """
    ttl_minutes = await DEMO_TOKEN_TTL_MINUTES()
    subject = f"demo-{uuid.uuid4().hex[:12]}"
    payload: Dict[str, Any] = {
        "sub": subject,
        "demo": True,
        "demo_session_id": session_id,
        "demo_message_cap": int(message_cap),
    }
    return jwt_manager.create_access_token(
        payload, expires_delta=timedelta(minutes=ttl_minutes)
    )


def _decode_or_401(token: str) -> Dict[str, Any]:
    """Verify the JWT signature + standard claims, or raise 401."""
    try:
        return pyjwt.decode(
            token,
            jwt_manager.secret_key,
            algorithms=[jwt_manager.algorithm],
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Demo session token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except pyjwt.InvalidTokenError as exc:
        logger.warning(f"demo_token: invalid token: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid demo session token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_demo_session(
    session_id: Annotated[str, Path(...)],
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_demo_bearer)],
) -> DemoSessionContext:
    """FastAPI dependency for ``/chat/demo/session/{session_id}/...`` routes.

    Verifies the bearer token is a *demo* token (``demo: true``) and that
    its bound ``demo_session_id`` matches the URL path. Either check
    failing yields 401 — same status as the standard RBAC dep so clients
    don't have to special-case demo-vs-real failures.
    """
    payload = _decode_or_401(creds.credentials)
    if not payload.get("demo"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a demo token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claim_session = payload.get("demo_session_id")
    if not isinstance(claim_session, str) or claim_session != session_id:
        # Don't reveal which session the token *was* for — that's a
        # potential reconnaissance signal in a public endpoint.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Demo token does not match this session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_cap = payload.get("demo_message_cap")
    try:
        message_cap = int(raw_cap) if raw_cap is not None else 0
    except (TypeError, ValueError):
        message_cap = 0

    return DemoSessionContext(
        session_id=session_id,
        message_cap=message_cap,
        subject=str(payload.get("sub", "")),
    )


__all__ = ["DemoSessionContext", "mint_demo_token", "require_demo_session"]
