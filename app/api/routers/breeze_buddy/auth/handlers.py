"""
Authentication handlers for breeze buddy.

This module contains the business logic for authentication operations:
- User login (database users + legacy hardcoded credentials)
- S2S token generation (long-lived tokens for server-to-server auth)
- User info retrieval
- Logout handling
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Response, status

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.config.static import (
    BREEZE_BUDDY_DASHBOARD_PASSWORD,
    BREEZE_BUDDY_DASHBOARD_USERNAME,
    BREEZE_BUDDY_SESSION_SECRET_KEY,
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
)
from app.core.logger import logger
from app.core.security.password import verify_password
from app.core.security.scope import resolve_merchant_ids, resolve_reseller_ids
from app.database.queries.breeze_buddy.users import get_user_by_username
from app.schemas import (
    LoginRequest,
    S2STokenRequest,
    S2STokenResponse,
    TokenResponse,
    UserInfo,
    UserRole,
)


async def login_handler(
    login_request: LoginRequest, response: Response
) -> TokenResponse:
    """
    Handle user login with JWT token-based authentication.

    Supports both:
    1. Database users with RBAC (new system)
    2. Hardcoded credentials for backward compatibility (legacy system)

    Args:
        login_request: Login credentials (username, password)
        response: FastAPI Response object for setting cookies

    Returns:
        TokenResponse with access_token, token_type, expires_in

    Raises:
        HTTPException: 401 if credentials are invalid or account is inactive
    """
    # Try database authentication first (new RBAC system)
    user = await get_user_by_username(login_request.username)

    if user:
        # Verify password first to prevent username enumeration
        # Both is_active and password checks return identical error messages
        if not verify_password(login_request.password, user.password_hash):
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
        # merchant_identifiers instead of ["*"] which it can't interpret.
        resolved_reseller_ids = user.reseller_ids
        resolved_merchant_identifiers = user.merchant_identifiers

        if user.role != UserRole.ADMIN:
            # Build a temporary UserInfo to use resolve functions
            temp_user_info = UserInfo(
                id=user.id,
                username=user.username,
                role=user.role,
                email=user.email,
                reseller_ids=user.reseller_ids,
                merchant_ids=user.reseller_ids,
                merchant_identifiers=user.merchant_identifiers,
                shop_identifiers=user.merchant_identifiers,
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
                resolved_merchant_identifiers = resolved_m
            # else: None means admin-created with truly unrestricted access,
            # keep original ["*"] — only admin-created accounts can reach here

        # Generate JWT token with resolved RBAC data
        access_token = rbac_token_manager.create_access_token_with_rbac(
            user_id=user.id,
            username=user.username,
            role=user.role,
            reseller_ids=resolved_reseller_ids,
            merchant_identifiers=resolved_merchant_identifiers,
            email=user.email,
            owner_id=user.owner_id,
        )

        # Calculate expires_in based on JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        expires_in = JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60  # Convert to seconds

        logger.info(
            f"Successful login for database user: {user.username} "
            f"(role: {user.role}, resellers: {user.reseller_ids}, shops: {user.merchant_identifiers})"
        )

        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",
            expires_in=expires_in,
        )

    # Fallback to hardcoded credentials (legacy system for backward compatibility)
    if (
        login_request.username == BREEZE_BUDDY_DASHBOARD_USERNAME
        and login_request.password == BREEZE_BUDDY_DASHBOARD_PASSWORD
    ):
        # Create legacy session cookie for old dashboard
        session_data = {
            "username": login_request.username,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        session_cookie = jwt.encode(
            session_data, BREEZE_BUDDY_SESSION_SECRET_KEY, algorithm=JWT_ALGORITHM
        )
        response.set_cookie(key="session", value=session_cookie, httponly=True)

        # Also create JWT token for API access (assume admin role)
        access_token = rbac_token_manager.create_access_token_with_rbac(
            user_id="legacy_admin",
            username=login_request.username,
            role=UserRole.ADMIN,
            reseller_ids=["*"],  # Admin has access to all resellers
            merchant_identifiers=["*"],  # Admin has access to all merchants
            email=None,
        )

        expires_in = JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60

        logger.info(f"Successful login for legacy admin: {login_request.username}")

        return TokenResponse(
            access_token=access_token,
            token_type="Bearer",
            expires_in=expires_in,
        )

    # Authentication failed
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
        logger.warning(f"S2S token request failed: user not found - {request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    # Verify password
    if not verify_password(request.password, user.password_hash):
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

    # Resolve wildcard scopes for S2S tokens too
    resolved_reseller_ids = user.reseller_ids
    resolved_merchant_identifiers = user.merchant_identifiers

    if user.role != UserRole.ADMIN:
        temp_user_info = UserInfo(
            id=user.id,
            username=user.username,
            role=user.role,
            email=user.email,
            reseller_ids=user.reseller_ids,
            merchant_ids=user.reseller_ids,
            merchant_identifiers=user.merchant_identifiers,
            shop_identifiers=user.merchant_identifiers,
            permissions=[],
            owner_id=user.owner_id,
        )

        resolved_r = await resolve_reseller_ids(temp_user_info)
        if resolved_r is not None:
            resolved_reseller_ids = resolved_r
        else:
            resolved_reseller_ids = ["*"]

        resolved_m = await resolve_merchant_ids(temp_user_info)
        if resolved_m is not None:
            resolved_merchant_identifiers = resolved_m
        else:
            resolved_merchant_identifiers = ["*"]

    access_token = rbac_token_manager.create_access_token_with_rbac(
        user_id=user.id,
        username=user.username,
        role=user.role,
        reseller_ids=resolved_reseller_ids,
        merchant_identifiers=resolved_merchant_identifiers,
        email=user.email,
        owner_id=user.owner_id,
        expires_delta=expires_delta,
    )

    # Calculate response metadata
    expires_in_seconds = int(expires_delta.total_seconds())
    expires_at = (datetime.now(timezone.utc) + expires_delta).isoformat()

    logger.info(
        f"S2S token generated successfully for user: {user.username} "
        f"(role: {user.role}, lifetime: {request.token_lifetime_days} days, "
        f"expires: {expires_at})"
    )

    return S2STokenResponse(
        success=True,
        access_token=access_token,
        token_type="Bearer",
        expires_in=expires_in_seconds,
        expires_at=expires_at,
        note=(
            f"Long-lived S2S token valid for {request.token_lifetime_days} days. "
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
