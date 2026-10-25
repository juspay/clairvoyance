"""
Breeze Buddy specific RBAC token management.
Handles JWT token creation and verification with Breeze Buddy's RBAC model.
"""

import hashlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection

from app.core.config.static import JWT_ACCESS_TOKEN_EXPIRE_MINUTES
from app.core.logger import logger
from app.core.security.jwt import jwt_manager
from app.database.accessor.breeze_buddy.merchants import (
    get_merchant_by_merchant_identifier,
)
from app.database.accessor.breeze_buddy.token_registry import (
    get_token_by_hash,
    mark_token_seen,
    register_token,
)
from app.database.accessor.breeze_buddy.users import get_user_token_state
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.token_registry import TokenKind, TokenRegistryEntry

# HTTP Bearer security scheme
security = HTTPBearer()

# A token that lives no longer than a login session (or the fixed 60-minute
# launch token) and carries no jti skips the registry: it is never registered
# and costs no DB read. Everything longer is a long-lived token.
_SHORT_LIVED_SECONDS = max(JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60, 3600)
# How long a pod trusts its last registry read of a token — the longest a
# revoke made on another pod takes to land here.
_REGISTRY_CACHE_SECONDS = 30.0
# last_seen_* is written at most this often per token.
_SEEN_WRITE_SECONDS = 300
# ponytail: the cache is cleared whole when full; long-lived tokens number in
# the hundreds, so it never fills in practice — an LRU if it ever does.
_REGISTRY_CACHE_MAX = 10_000
_SEEN_FIELD_MAX_CHARS = 200
# token hash -> (time.monotonic() of the read, registry row)
_registry_cache: Dict[str, Tuple[float, TokenRegistryEntry]] = {}
# The subject of a per-merchant (and launch) token is "merchant:<id>".
_MERCHANT_SUBJECT_PREFIX = "merchant:"


class _Subject(NamedTuple):
    """What the verifier knows about a token's subject (user or merchant)."""

    found: bool
    active: bool
    tokens_valid_after: Optional[datetime]


# subject -> (time.monotonic() of the read, its state); same window as above.
_subject_cache: Dict[str, Tuple[float, _Subject]] = {}


def _unauthorized() -> HTTPException:
    """The verifier's one refusal for a token it won't accept."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def token_times(token: str) -> Tuple[datetime, datetime]:
    """A token's own iat and exp, read back from the token (no verify:
    callers hold a token they just minted)."""
    claims = pyjwt.decode(token, options={"verify_signature": False})
    return (
        datetime.fromtimestamp(claims["iat"], timezone.utc),
        datetime.fromtimestamp(claims["exp"], timezone.utc),
    )


def token_hash_of(token: str) -> str:
    """The registry key of a token: hex sha256 of the token string."""
    return hashlib.sha256(token.encode()).hexdigest()


def _is_long_lived(claims: Dict[str, Any]) -> bool:
    """A jti, or a lifetime past a login session. No iat → long-lived."""
    if claims.get("jti"):
        return True
    iat = claims.get("iat")
    return iat is None or claims["exp"] - iat > _SHORT_LIVED_SECONDS


def _seen_from(connection: Optional[HTTPConnection]) -> Optional[str]:
    """The X-Forwarded-For chain as received, else the direct peer."""
    if connection is None:
        return None
    chain = connection.headers.get("x-forwarded-for")
    if not chain and connection.client:
        chain = connection.client.host
    return chain[:_SEEN_FIELD_MAX_CHARS] if chain else None


def _route_of(connection: Optional[HTTPConnection]) -> Optional[str]:
    """The route template (no ids in it) when known, else the path."""
    if connection is None:
        return None
    path = getattr(connection.scope.get("route"), "path", None) or connection.url.path
    return path[:_SEEN_FIELD_MAX_CHARS]


class BreezeBuddyRBACTokenManager:
    """Breeze Buddy specific RBAC token manager."""

    def __init__(self):
        self.jwt_manager = jwt_manager

    def create_access_token_with_rbac(
        self,
        user_id: str,
        username: str,
        role: UserRole,
        reseller_ids: List[str],
        merchant_ids: List[str],
        email: Optional[str] = None,
        owner_id: Optional[str] = None,
        expires_delta: Optional[timedelta] = None,
        src: Optional[str] = None,
        jti: Optional[str] = None,
    ) -> str:
        """
        Create a JWT access token with Breeze Buddy RBAC information.

        Args:
            user_id: User's unique identifier
            username: Username
            role: User's role (admin, reseller, merchant, user)
            reseller_ids: List of accessible reseller IDs (["*"] for all resellers)
            merchant_ids: List of accessible merchant identifiers (["*"] for all merchants under the reseller(s))
            email: User's email (optional)
            expires_delta: Optional custom expiration time
            src: Optional provenance marker recorded on the token (e.g.
                "nautilus" for Shopify-admin launch tokens)
            jti: Optional token id — the token_registry row id, set only by
                ``create_registered_token``

        Returns:
            Encoded JWT token string with RBAC data
        """
        # Get permissions based on role
        permissions = self._get_permissions_for_role(role)

        # Build JWT payload with Breeze Buddy RBAC data
        payload = {
            "sub": user_id,
            "username": username,
            "role": role.value if isinstance(role, UserRole) else role,
            "email": email,
            "reseller_ids": reseller_ids,
            "merchant_ids": merchant_ids,
            "permissions": permissions,
            "owner_id": owner_id,
        }

        if src:
            payload["src"] = src
        if jti:
            payload["jti"] = jti

        # Use the generic JWT manager to create the token
        return self.jwt_manager.create_access_token(payload, expires_delta)

    async def create_registered_token(
        self,
        *,
        kind: TokenKind,
        created_by: Optional[str],
        user_id: str,
        username: str,
        role: UserRole,
        reseller_ids: List[str],
        merchant_ids: List[str],
        expires_delta: timedelta,
        email: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> str:
        """Mint a long-lived RBAC token and write its token_registry row.

        The token carries the row's id as ``jti``; the row holds the token's
        sha256 hash (never the token) and its exact ``iat``/``exp``. Fails
        CLOSED: if the row is not written, the token is never returned, so
        no long-lived token exists that the registry doesn't know about.
        """
        jti = str(uuid.uuid4())
        token = self.create_access_token_with_rbac(
            user_id=user_id,
            username=username,
            role=role,
            reseller_ids=reseller_ids,
            merchant_ids=merchant_ids,
            email=email,
            owner_id=owner_id,
            expires_delta=expires_delta,
            jti=jti,
        )
        # Read iat/exp back from the token itself, so the row matches the
        # claims to the second rather than to a second clock read.
        issued_at, expires_at = token_times(token)
        try:
            await register_token(
                token_id=jti,
                token_hash=token_hash_of(token),
                kind=kind,
                source="minted",
                subject=user_id,
                username=username,
                role=role.value if isinstance(role, UserRole) else role,
                reseller_ids=reseller_ids,
                merchant_ids=merchant_ids,
                created_by=created_by,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not create access token",
            )
        return token

    async def authenticate(
        self, token: str, connection: Optional[HTTPConnection] = None
    ) -> UserInfo:
        """``verify_rbac_token``, then the token's subject, then the token
        registry (enforced).

        Every token's subject — a user, or ``merchant:<id>`` — must exist and
        be active (401 otherwise). A short-lived token must also be issued
        after its user's ``tokens_valid_after`` (the login cut-off).

        A long-lived token (see ``_is_long_lived``) must be in the registry,
        looked up by hash: unknown → 401, revoked → 401. Rows come from the
        mint, from rotation, and from the observe period of 2/4 — a token the
        registry has never seen is no longer let through.

        Any lookup that can't be made → 503: a refusal (fail CLOSED), but not
        one a caller would read as "your token is bad". ``connection`` (the
        request or websocket) only feeds last_seen_*.
        """
        user_info = self.verify_rbac_token(token)
        # Already verified above; exp is not re-checked so a token expiring
        # between the two reads can't turn into a 500.
        claims = pyjwt.decode(
            token,
            self.jwt_manager.secret_key,
            algorithms=[self.jwt_manager.algorithm],
            options={"verify_exp": False},
        )
        long_lived = _is_long_lived(claims)
        await self._check_subject(claims, long_lived)
        if not long_lived:
            return user_info

        token_hash = token_hash_of(token)
        entry = await self._registry_row(token_hash)
        if entry is None:
            logger.warning(
                f"RBAC verifier: rejected unregistered token of {claims['sub']} "
                f"({claims['username']}, jti: {claims.get('jti') or '-'})"
            )
            raise _unauthorized()
        if entry.revoked_at is not None and entry.revoked_at <= datetime.now(
            timezone.utc
        ):
            logger.warning(
                f"RBAC verifier: rejected revoked token {entry.id} ({entry.username})"
            )
            raise _unauthorized()
        await self._note_seen(token_hash, entry, connection)
        return user_info

    async def _registry_row(self, token_hash: str) -> Optional[TokenRegistryEntry]:
        """This pod's recent read of the token's row, else a fresh one;
        None when the registry has no such token (not cached, so a token
        minted on another pod a moment ago is found on its next use)."""
        cached = _registry_cache.get(token_hash)
        now = time.monotonic()
        if cached and now - cached[0] < _REGISTRY_CACHE_SECONDS:
            return cached[1]
        try:
            entry = await get_token_by_hash(token_hash)
        except Exception:
            logger.error("RBAC verifier: token registry unavailable — refusing")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
            )
        if entry is None:
            return None
        if len(_registry_cache) >= _REGISTRY_CACHE_MAX:
            _registry_cache.clear()
        _registry_cache[token_hash] = (now, entry)
        return entry

    async def _note_seen(
        self,
        token_hash: str,
        entry: TokenRegistryEntry,
        connection: Optional[HTTPConnection],
    ) -> None:
        """Stamp last_seen_* at most every ``_SEEN_WRITE_SECONDS``.

        Bookkeeping, never a decision: a failed write is logged by the
        accessor and the request goes on. Either way the cached row takes
        the new stamp, so a failing write isn't retried on every request.
        """
        now = datetime.now(timezone.utc)
        if (
            entry.last_seen_at is not None
            and (now - entry.last_seen_at).total_seconds() < _SEEN_WRITE_SECONDS
        ):
            return
        stamped: Optional[datetime] = None
        try:
            stamped = await mark_token_seen(
                entry.id, _seen_from(connection), _route_of(connection)
            )
        except Exception:
            pass
        cached = _registry_cache.get(token_hash)
        if cached:
            _registry_cache[token_hash] = (
                cached[0],
                cached[1].model_copy(update={"last_seen_at": stamped or now}),
            )

    def forget(self, token_hash: str) -> None:
        """Drop this pod's cached read of a token (after a revoke here)."""
        _registry_cache.pop(token_hash, None)

    def forget_subject(self, subject: str) -> None:
        """Drop this pod's cached read of a user (its id) or a merchant
        (``merchant:<id>``) after deactivating it or cutting its sessions."""
        _subject_cache.pop(subject, None)

    async def _check_subject(self, claims: Dict[str, Any], long_lived: bool) -> None:
        """Refuse a token whose user or merchant is gone or inactive, or a
        login token issued before its user's cut-off."""
        subject = str(claims["sub"])
        state = await self._subject_state(subject)
        if not state.found:
            logger.warning(f"RBAC verifier: rejected token of unknown {subject}")
            raise _unauthorized()
        if not state.active:
            logger.warning(f"RBAC verifier: rejected token of inactive {subject}")
            raise _unauthorized()
        if long_lived or state.tokens_valid_after is None:
            return
        iat = claims.get("iat")
        # iat is whole seconds: a login in the same second as the cut-off is
        # refused too (fail closed) — the user signs in again a second later.
        if iat is None or (
            datetime.fromtimestamp(iat, timezone.utc) < state.tokens_valid_after
        ):
            logger.warning(f"RBAC verifier: rejected cut-off login token of {subject}")
            raise _unauthorized()

    async def _subject_state(self, subject: str) -> _Subject:
        """This pod's recent read of a subject, else a fresh one."""
        cached = _subject_cache.get(subject)
        now = time.monotonic()
        if cached and now - cached[0] < _REGISTRY_CACHE_SECONDS:
            return cached[1]
        try:
            if subject.startswith(_MERCHANT_SUBJECT_PREFIX):
                merchant = await get_merchant_by_merchant_identifier(
                    subject[len(_MERCHANT_SUBJECT_PREFIX) :]
                )
                state = _Subject(
                    merchant is not None,
                    bool(merchant and merchant.is_active),
                    None,
                )
            else:
                user = await get_user_token_state(subject)
                state = _Subject(
                    user is not None,
                    bool(user and user.is_active),
                    user.tokens_valid_after if user else None,
                )
        except Exception:
            logger.error("RBAC verifier: token subject unreadable — refusing")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
            )
        if len(_subject_cache) >= _REGISTRY_CACHE_MAX:
            _subject_cache.clear()
        _subject_cache[subject] = (now, state)
        return state

    def verify_rbac_token(self, token: str) -> UserInfo:
        """
        Verify and decode a JWT token with Breeze Buddy RBAC information.

        Args:
            token: JWT token string

        Returns:
            UserInfo object containing user and RBAC information

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            payload = pyjwt.decode(
                token,
                self.jwt_manager.secret_key,
                algorithms=[self.jwt_manager.algorithm],
            )

            # Reject non-RBAC token types up front. Widget tokens (typ="widget",
            # see ``widget_token.py``) and demo tokens (``demo: true``, see
            # ``demo_token.py``) are signed by the same key but must never be
            # accepted on RBAC routes — they carry no reseller/merchant scopes
            # and would otherwise authenticate as a UserInfo with empty
            # arrays. All existing RBAC/s2s tokens are minted without these
            # claims, so the check is backward-compatible.
            if payload.get("typ") == "widget":
                logger.warning("RBAC verifier: rejected widget token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if payload.get("demo") is True:
                logger.warning("RBAC verifier: rejected demo token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Check if token has expired (redundant with jwt.decode but explicit)
            exp = payload.get("exp")
            if exp is None:
                logger.warning("Token missing expiration claim")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Validate required identity claims
            user_id = payload.get("sub")
            username = payload.get("username")
            if not user_id or not username:
                logger.warning("Token missing required identity claims (sub/username)")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if user_id == "legacy_admin":
                logger.warning("RBAC verifier: rejected legacy admin token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Extract Breeze Buddy RBAC data
            # Normalize legacy "shop" role to "user" for old JWTs still in circulation
            role_str = payload.get("role")
            if role_str == "shop":
                role_str = "user"

            user_info = UserInfo(
                id=user_id,
                username=username,
                role=UserRole(role_str),
                email=payload.get("email"),
                reseller_ids=payload.get("reseller_ids", []),
                merchant_ids=payload.get("merchant_ids", []),
                permissions=payload.get("permissions", []),
                owner_id=payload.get("owner_id"),
            )

            logger.info(
                f"RBAC token verified for user: {user_info.username} "
                f"(role: {user_info.role}, resellers: {user_info.reseller_ids}, merchants: {user_info.merchant_ids}, "
                f"jti: {payload.get('jti') or '-'})"
            )
            return user_info

        except pyjwt.ExpiredSignatureError:
            logger.warning("JWT token has expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except pyjwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except ValueError as e:
            logger.warning(f"Invalid role in token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Unexpected error verifying RBAC token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def _get_permissions_for_role(self, role: UserRole) -> List[str]:
        """
        Get Breeze Buddy permissions list based on user role.

        Args:
            role: User's role

        Returns:
            List of permission strings for Breeze Buddy
        """
        role_str = role.value if isinstance(role, UserRole) else role

        if role_str == "admin":
            return [
                "read:all",
                "write:all",
                "delete:all",
                "analytics:all",
                "configurations:all",
                "merchants:all",
            ]
        elif role_str == "reseller":
            return [
                "read:assigned_merchants",
                "write:assigned_merchants",
                "analytics:assigned_merchants",
                "configurations:assigned_merchants",
            ]
        elif role_str == "merchant":
            return [
                "read:own_data",
                "write:own_data",
                "analytics:own",
                "configurations:read",
            ]
        elif role_str == "user":
            return [
                "read:own_data",
                "analytics:own",
            ]
        else:
            return []


# Initialize Breeze Buddy RBAC token manager
rbac_token_manager = BreezeBuddyRBACTokenManager()


# FastAPI dependency for Breeze Buddy RBAC authentication
async def get_current_user_with_rbac(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserInfo:
    """
    FastAPI dependency to get current authenticated user with Breeze Buddy RBAC information.

    Args:
        request: The request (feeds the token registry's last_seen_*)
        credentials: HTTP Bearer credentials from request header

    Returns:
        UserInfo object containing user and RBAC information

    Raises:
        HTTPException: If token is invalid or missing
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await rbac_token_manager.authenticate(credentials.credentials, request)


async def get_user_from_websocket(websocket: WebSocket) -> UserInfo:
    """Authenticate a WebSocket connection with the standard RBAC bearer token.

    The FastAPI ``Depends(HTTPBearer())`` guards are HTTP-only, so WebSocket
    routes call this directly after ``accept()``. Token sources, in order:
    the ``Authorization: Bearer`` header, then a ``token`` query parameter
    (browser WebSocket clients cannot set headers).

    Raises:
        HTTPException: 401 when the token is missing, invalid or revoked; 503
        when the token registry can't be read — callers translate either into
        a WebSocket close.
    """
    auth_header = websocket.headers.get("authorization") or ""
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[len("bearer ") :].strip()
    if not token:
        token = (websocket.query_params.get("token") or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
        )
    return await rbac_token_manager.authenticate(token, websocket)


async def get_active_user(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UserInfo:
    """
    FastAPI dependency to get current active user.

    Args:
        current_user: Current user from token

    Returns:
        UserInfo if user is active

    Raises:
        HTTPException: If user is inactive
    """
    # Note: is_active check would need database lookup
    # For now, we assume token = active user
    return current_user
