"""Widget-session JWT — mint + verify for the unified widget endpoints.

Why a separate token type instead of reusing the standard RBAC JWT or
the demo token (see ``demo_token.py``):

- Widget endpoints (``/widget/session/*``) are reached anonymously
  from a merchant's browser embed. Like the demo, they have no
  upstream user; unlike the demo, they are production traffic gated
  by a per-merchant ``widget_config`` row instead of a hardcoded slug
  allowlist.
- A widget JWT carries ``typ: "widget"``. The standard RBAC verifier
  (``rbac_token.py``) is hardened to reject ``typ == "widget"`` so a
  leaked widget token cannot drive any RBAC endpoint. The demo
  verifier (``demo_token.py``) already rejects non-demo tokens by
  checking ``demo: true``.
- One token, one session, both channels. The conversation backbone is
  a single ``chat_session`` row that voice can attach to as a
  transient. The token binds to ``widget_session_id`` and stays valid
  across chat → voice → chat handoffs — no re-mint needed (see
  ``docs/CHAT_MODE.md`` §14 for the unified-session model).
- Pinned to ``widget_session_id``: the dependency rejects any
  path-param ``session_id`` that doesn't match — so a leaked token
  can't be steered to other sessions.

TTL default is 24 h. Voice attachments inside that 24 h are bounded
separately by Daily room expiry (1 h).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Any, Dict

import jwt as pyjwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.logger import logger
from app.core.security.jwt import jwt_manager

# Shared HTTPBearer scheme so the OpenAPI doc shows one consistent
# Authorization header across widget routes. ``auto_error=True`` gives
# us 403 when the header is missing — matches RBAC/demo behaviour.
_widget_bearer = HTTPBearer(auto_error=True, scheme_name="WidgetBearer")

TYP_WIDGET = "widget"

# Default lifetime — see module docstring.
DEFAULT_WIDGET_TOKEN_TTL_MINUTES = 24 * 60


class WidgetSessionContext:
    """Resolved widget-token claims, returned by ``require_widget_session``."""

    __slots__ = ("session_id", "widget_config_id", "subject")

    def __init__(self, *, session_id: str, widget_config_id: str, subject: str) -> None:
        self.session_id = session_id
        self.widget_config_id = widget_config_id
        self.subject = subject


def mint_widget_token(
    *,
    widget_session_id: str,
    widget_config_id: str,
    ttl_minutes: int = DEFAULT_WIDGET_TOKEN_TTL_MINUTES,
) -> str:
    """Issue a widget JWT bound to ``widget_session_id``.

    ``sub`` is a fresh anonymous identifier (``widget-<short-uuid>``) —
    useful in logs but never used for authorization. The real gates are
    the ``typ`` and ``widget_session_id`` claims verified by
    ``require_widget_session``.

    Synchronous: callers know the TTL they want (24h for widget mode).
    """
    subject = f"widget-{uuid.uuid4().hex[:12]}"
    payload: Dict[str, Any] = {
        "sub": subject,
        "typ": TYP_WIDGET,
        "widget_session_id": widget_session_id,
        "widget_config_id": widget_config_id,
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
            detail="Widget session token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except pyjwt.InvalidTokenError as exc:
        logger.warning(f"widget_token: invalid token: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid widget session token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_widget_session(
    session_id: Annotated[str, Path(...)],
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_widget_bearer)],
) -> WidgetSessionContext:
    """FastAPI dependency for ``/widget/session/{session_id}/...`` routes.

    Verifies the bearer token is a *widget* token (``typ=widget``) and
    that its bound ``widget_session_id`` matches the URL path. Either
    check failing yields 401 — same status as the standard RBAC /
    demo deps so clients don't have to special-case widget failures.
    """
    payload = _decode_or_401(creds.credentials)

    if payload.get("typ") != TYP_WIDGET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a widget token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claim_session = payload.get("widget_session_id")
    if not isinstance(claim_session, str) or claim_session != session_id:
        # Don't echo back which session the token *was* for — small
        # reconnaissance hardening on a public endpoint.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget token does not match this session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    widget_config_id = payload.get("widget_config_id")
    if not isinstance(widget_config_id, str) or not widget_config_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Widget token missing widget_config_id",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return WidgetSessionContext(
        session_id=session_id,
        widget_config_id=widget_config_id,
        subject=str(payload.get("sub", "")),
    )


def assert_widget_session_ownership(session: Any, ctx: WidgetSessionContext) -> None:
    """Verify the chat_session was created by the same widget_config the
    bearer token claims. Returns silently on match; raises 404 on
    mismatch (or on a malformed metadata payload).

    Why a separate assert instead of folding into ``require_widget_session``:
    the dep runs before the session is loaded — it only sees the path
    param + the token claims. The DB-side proof "this session row belongs
    to widget_config X" can only be checked after a SELECT, so each
    handler runs it explicitly right after ``get_chat_session_by_id``.

    Why 404 (not 403): leaking "this session exists but isn't yours"
    gives an attacker who's already harvested session UUIDs a probe
    oracle. We treat ownership mismatch and not-found identically.

    The check defends against a leaked widget token being steered at
    any session UUID the attacker has discovered (logs, error pages,
    metrics) — without this assert, `widget_session_id`-claim binding
    alone is insufficient.
    """
    try:
        widget_meta = (
            session.metadata.get("widget") if session and session.metadata else None
        )
        owning_config_id = widget_meta.get("widget_config_id") if widget_meta else None
    except AttributeError:
        owning_config_id = None
    if owning_config_id != ctx.widget_config_id:
        logger.warning(
            f"widget ownership mismatch: token widget_config_id={ctx.widget_config_id} "
            f"vs session metadata={owning_config_id!r} (session_id={ctx.session_id})"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Widget session '{ctx.session_id}' not found",
        )


__all__ = [
    "DEFAULT_WIDGET_TOKEN_TTL_MINUTES",
    "TYP_WIDGET",
    "WidgetSessionContext",
    "assert_widget_session_ownership",
    "mint_widget_token",
    "require_widget_session",
]
