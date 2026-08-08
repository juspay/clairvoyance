"""
Authentication handlers for breeze buddy.

This module contains the business logic for authentication operations:
- User login (database users)
- S2S token generation (long-lived tokens for server-to-server auth)
- User info retrieval
- Logout handling
"""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import HTTPException, status

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.config.static import (
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    LOOM_APP_URL,
)
from app.core.logger import logger
from app.core.security.password import DUMMY_PASSWORD_HASH, verify_password_async
from app.core.security.scope import resolve_merchant_ids, resolve_reseller_ids
from app.database.accessor.breeze_buddy import merchants as merchant_accessors
from app.database.accessor.breeze_buddy.users import get_user_by_username
from app.schemas import (
    LaunchTokenRequest,
    LaunchTokenResponse,
    LoginRequest,
    S2STokenRequest,
    S2STokenResponse,
    TokenResponse,
    UserInfo,
    UserRole,
)

# Relative-path guard for the `redirect` param: strict allowlist rather than
# a denylist. Must start with a single leading slash (not `//`), and contain
# only a restricted character set. This intentionally rejects backslashes,
# so WHATWG-URL-parser tricks like "/\evil.com" (which browsers normalize to
# the protocol-relative "//evil.com" for special-scheme URLs) never validate.
# Must match the equivalent guard in loom-v2 — the two services validate
# independently and must not diverge.
_REDIRECT_PATTERN = re.compile(r"^/(?!/)[A-Za-z0-9._~/-]*$")
_REDIRECT_MAX_LENGTH = 512

DEFAULT_LAUNCH_REDIRECT = "/home"

# Upstream products allowed to mint a Loom launch token. The value is stamped
# into the token's signed `src` claim (so it cannot be tampered with) and Loom
# gates sign-in on it. Adding a new integration (e.g. euler, lighthouse) is a
# one-line change here — Loom must separately enable it on its accept side.
KNOWN_LAUNCH_SOURCES = frozenset({"nautilus"})


async def login_handler(
    login_request: LoginRequest,
) -> TokenResponse:
    """
    Handle user login with JWT token-based authentication.

    Supports database users with RBAC.

    Args:
        login_request: Login credentials (username, password)

    Returns:
        TokenResponse with access_token, token_type, expires_in

    Raises:
        HTTPException: 401 if credentials are invalid or account is inactive
    """
    user = await get_user_by_username(login_request.username)

    if user:
        # Verify password first to prevent username enumeration
        # Both is_active and password checks return identical error messages
        if not await verify_password_async(login_request.password, user.password_hash):
            logger.warning(f"Failed login attempt for user: {login_request.username}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )

        # Check account status after password verification
        if not user.is_active:
            logger.warning(f"Inactive user attempted login: {login_request.username}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )

        # Resolve wildcard scopes to actual values before creating token.
        # This ensures the frontend receives concrete reseller_ids and
        # merchant_ids instead of ["*"] which it can't interpret.
        resolved_reseller_ids = user.reseller_ids
        resolved_merchant_ids = user.merchant_ids

        if user.role != UserRole.ADMIN:
            # Build a temporary UserInfo to use resolve functions
            temp_user_info = UserInfo(
                id=user.id,
                username=user.username,
                role=user.role,
                email=user.email,
                reseller_ids=user.reseller_ids,
                merchant_ids=user.merchant_ids,
                permissions=[],
                owner_id=user.owner_id,
            )

            # Resolve reseller_ids wildcards
            resolved_r = await resolve_reseller_ids(temp_user_info)
            if resolved_r is not None:
                resolved_reseller_ids = resolved_r
            # else: None means admin-created with truly unrestricted access,
            # keep original ["*"] — only admin-created accounts can reach here

            # Resolve merchant_identifiers wildcards
            resolved_m = await resolve_merchant_ids(temp_user_info)
            if resolved_m is not None:
                resolved_merchant_ids = resolved_m
            # else: None means admin-created with truly unrestricted access,
            # keep original ["*"] — only admin-created accounts can reach here

        # Generate JWT token with resolved RBAC data
        access_token = rbac_token_manager.create_access_token_with_rbac(
            user_id=user.id,
            username=user.username,
            role=user.role,
            reseller_ids=resolved_reseller_ids,
            merchant_ids=resolved_merchant_ids,
            email=user.email,
            owner_id=user.owner_id,
        )

        # Calculate expires_in based on JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        expires_in = JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60  # Convert to seconds

        logger.info(
            f"Successful login for database user: {user.username} "
            f"(role: {user.role}, resellers: {user.reseller_ids}, shops: {user.merchant_ids})"
        )

        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",
            expires_in=expires_in,
        )

    # Authentication failed: no such user. Still spend one bcrypt against a
    # dummy hash so an unknown username can't be distinguished from a wrong
    # password by response timing (username-enumeration oracle, PT-16). Uses the
    # async wrapper for the same reason the real check does: this branch is the
    # one an attacker can drive at will, so running bcrypt inline here would
    # freeze the event loop for ~240ms per guess.
    await verify_password_async(login_request.password, DUMMY_PASSWORD_HASH)
    logger.warning(f"Failed login attempt for unknown user: {login_request.username}")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password",
    )


async def generate_s2s_token_handler(request: S2STokenRequest) -> S2STokenResponse:
    """
    Generate long-lived token for Server-to-Server (S2S) authentication.

    Allows admin users to generate long-lived JWT tokens (up to 365 days)
    for automated integrations and S2S communication.

    Security restrictions:
    - Only admin users can generate S2S tokens
    - Maximum token lifetime: 365 days

    Args:
        request: S2STokenRequest containing username, password, and token_lifetime_days

    Returns:
        S2STokenResponse with access_token, expires_in, expires_at

    Raises:
        HTTPException: 401 if credentials are invalid or account is inactive
        HTTPException: 403 if user is not an admin
    """
    # Authenticate user
    user = await get_user_by_username(request.username)
    if not user:
        # Spend one bcrypt against a dummy hash before failing so an unknown
        # username is timing-indistinguishable from a wrong password — this path
        # would otherwise reveal whether an admin account exists (PT-16).
        await verify_password_async(request.password, DUMMY_PASSWORD_HASH)
        logger.warning(f"S2S token request failed: user not found - {request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    # Verify password
    if not await verify_password_async(request.password, user.password_hash):
        logger.warning(
            f"S2S token request failed: invalid password - {request.username}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    # Check if user is active
    if not user.is_active:
        logger.warning(f"S2S token request failed: inactive user - {request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive. Please contact administrator.",
        )

    # Security restriction: Only admins can generate S2S tokens
    if user.role != UserRole.ADMIN:
        logger.warning(
            f"S2S token request denied: non-admin user - {request.username} (role: {user.role})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can generate S2S tokens. Please contact your administrator.",
        )

    # Generate long-lived token
    expires_delta = timedelta(days=request.token_lifetime_days)

    # Start with the admin's own scopes
    resolved_reseller_ids = user.reseller_ids
    resolved_merchant_ids = user.merchant_ids

    # Apply caller-requested scope restrictions.
    # Prevent escalation: requested scopes must be a subset of the admin's own scopes.
    if request.reseller_ids is not None:
        if "*" not in resolved_reseller_ids:
            invalid = set(request.reseller_ids) - set(resolved_reseller_ids)
            if invalid:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot grant reseller access beyond your own scope: {sorted(invalid)}",
                )
        resolved_reseller_ids = request.reseller_ids

    if request.merchant_ids is not None:
        if "*" not in resolved_merchant_ids:
            invalid = set(request.merchant_ids) - set(resolved_merchant_ids)
            if invalid:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot grant merchant access beyond your own scope: {sorted(invalid)}",
                )
        resolved_merchant_ids = request.merchant_ids

    # If scopes are restricted (not wildcard), downgrade the token role
    # from ADMIN to RESELLER. RBAC checks short-circuit on role == "admin"
    # before inspecting reseller_ids/merchant_ids, so an ADMIN-role token
    # would bypass those restrictions entirely.
    scopes_are_restricted = (
        "*" not in resolved_reseller_ids or "*" not in resolved_merchant_ids
    )
    token_role = UserRole.RESELLER if scopes_are_restricted else user.role

    access_token = rbac_token_manager.create_access_token_with_rbac(
        user_id=user.id,
        username=user.username,
        role=token_role,
        reseller_ids=resolved_reseller_ids,
        merchant_ids=resolved_merchant_ids,
        email=user.email,
        owner_id=user.owner_id,
        expires_delta=expires_delta,
    )

    # Calculate response metadata
    expires_in_seconds = int(expires_delta.total_seconds())
    expires_at = (datetime.now(timezone.utc) + expires_delta).isoformat()

    logger.info(
        f"S2S token generated for user: {user.username} "
        f"(auth_role: {user.role}, token_role: {token_role}, "
        f"lifetime: {request.token_lifetime_days} days)"
    )

    return S2STokenResponse(
        success=True,
        access_token=access_token,
        token_type="Bearer",
        expires_in=expires_in_seconds,
        expires_at=expires_at,
        note=(
            f"Long-lived S2S token valid for {request.token_lifetime_days} days "
            f"(effective role: {token_role.value}). "
            f"Store securely and rotate before expiration!"
        ),
    )


async def get_user_info_handler(current_user: UserInfo) -> UserInfo:
    """
    Get current authenticated user information from JWT token.

    Args:
        current_user: UserInfo extracted from JWT token

    Returns:
        UserInfo object with user details
    """
    logger.info(
        f"User info request from {current_user.username} (role: {current_user.role})"
    )
    return current_user


async def _check_launch_token_access(current_user: UserInfo, reseller_id: str) -> None:
    """Gate access to launch-token minting.

    - Admin: can mint for any reseller/merchant.
    - Reseller: can only mint for a reseller_id within their own resolved scope
      (mirrors the owning-reseller check used for merchant create/update).
    - Anyone else: forbidden — this endpoint impersonates a merchant session
      and must not be reachable by merchant/user-role tokens.
    """
    if current_user.role == UserRole.ADMIN:
        return

    if current_user.role != UserRole.RESELLER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins and resellers can mint launch tokens",
        )

    allowed_reseller_ids = await resolve_reseller_ids(current_user)
    if allowed_reseller_ids is not None and reseller_id not in allowed_reseller_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only mint launch tokens for resellers you own",
        )


async def launch_token_handler(
    request: LaunchTokenRequest, current_user: UserInfo
) -> LaunchTokenResponse:
    """
    Mint a 1-hour merchant-scoped Loom session JWT for the Nautilus connector.

    Verifies the caller (admin or owning reseller) has scope over the
    requested reseller, that the merchant exists/is active/belongs to that
    reseller, then mints a virtual-subject merchant token
    (``sub=f"merchant:{merchant_id}"``, no real ``users`` row) with a signed
    ``src`` claim (the requesting product) that Loom gates sign-in on.

    Args:
        request: reseller_id, merchant_id, source, and optional redirect path
        current_user: Authenticated caller (admin or reseller)

    Returns:
        LaunchTokenResponse with access_token, expires_in, and launch_url

    Raises:
        HTTPException: 403 if caller lacks scope for the reseller
        HTTPException: 404 if merchant doesn't exist / is inactive / belongs
            to a different reseller
        HTTPException: 400 if source is unknown, or redirect is present but not
            a safe relative path
    """
    if request.source not in KNOWN_LAUNCH_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown launch source: {request.source!r}",
        )

    await _check_launch_token_access(current_user, request.reseller_id)

    merchant = await merchant_accessors.get_merchant_by_merchant_identifier(
        request.merchant_id
    )
    if (
        not merchant
        or not merchant.is_active
        or merchant.reseller_id != request.reseller_id
    ):
        raise HTTPException(status_code=404, detail="Merchant not found")

    redirect = (
        request.redirect if request.redirect is not None else DEFAULT_LAUNCH_REDIRECT
    )
    if len(redirect) > _REDIRECT_MAX_LENGTH or not _REDIRECT_PATTERN.match(redirect):
        raise HTTPException(
            status_code=400,
            detail="Invalid redirect: must be a relative path (e.g. /home)",
        )

    access_token = rbac_token_manager.create_access_token_with_rbac(
        user_id=f"merchant:{request.merchant_id}",
        username=f"merchant-{request.merchant_id}",
        role=UserRole.MERCHANT,
        reseller_ids=[request.reseller_id],
        merchant_ids=[request.merchant_id],
        owner_id=current_user.id,
        src=request.source,
        # Pin the impersonation window to a fixed 60 minutes rather than
        # inheriting JWT_ACCESS_TOKEN_EXPIRE_MINUTES: this token grants a live
        # merchant session, so its TTL must not silently drift if the shared
        # login-session config is later retuned.
        expires_delta=timedelta(minutes=60),
    )

    expires_in = 60 * 60  # fixed 60-minute window, decoupled from login config

    launch_url = (
        f"{LOOM_APP_URL.rstrip('/')}/launch"
        f"?token={quote(access_token, safe='')}"
        f"&redirect={quote(redirect, safe='')}"
    )

    # Mandatory audit log: launch-token minting is impersonation by design
    # (an admin/reseller-token holder mints a live merchant session), so every
    # mint must be attributable to the caller who requested it.
    logger.info(
        f"Launch token minted by caller={current_user.id} "
        f"({current_user.username}, role={current_user.role}) "
        f"for reseller_id={request.reseller_id} merchant_id={request.merchant_id}"
    )

    return LaunchTokenResponse(
        access_token=access_token,
        token_type="Bearer",
        expires_in=expires_in,
        launch_url=launch_url,
    )


async def logout_handler() -> dict:
    """
    Handle logout for JWT token-based authentication.

    Since JWT tokens are stateless and stored client-side:
    - Backend cannot invalidate the token (no session to destroy)
    - Client must delete the token from localStorage/cookies
    - Token will naturally expire after its lifetime

    Returns:
        Success message with logout instructions
    """
    logger.info("Logout endpoint called (token-based auth - client-side logout)")

    return {
        "success": True,
        "message": "Logout acknowledged. Client should clear token from storage.",
        "instructions": {
            "step_1": "Remove token from localStorage or cookies",
            "step_2": "Clear user state in your application",
            "step_3": "Redirect to login page",
            "note": "Token remains valid until expiration but client discards it",
        },
    }
