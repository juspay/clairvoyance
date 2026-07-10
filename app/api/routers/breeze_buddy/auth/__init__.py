"""
Modern authentication endpoints with RBAC.

This module provides JWT token-based authentication endpoints for breeze buddy.

Endpoints:
- POST   /login              - Authenticate user and get JWT token
- POST   /auth/s2s/token     - Generate long-lived S2S token (admin only)
- GET    /auth/me            - Get current user info from token
- POST   /auth/logout        - Logout (client-side token removal)
- GET    /logout             - Legacy logout endpoint (deprecated)

For backward compatibility, old session-based logout is also supported.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import (
    LoginRequest,
    S2STokenRequest,
    S2STokenResponse,
    TokenResponse,
    UserInfo,
)

from .handlers import (
    generate_s2s_token_handler,
    get_user_info_handler,
    login_handler,
    logout_handler,
)

router = APIRouter()


@router.post("/login", include_in_schema=False, response_model=TokenResponse)
async def login(login_request: LoginRequest):
    """
    Login endpoint with JWT token-based authentication.

    This endpoint supports database users with RBAC.

    Returns JWT access token with user information and RBAC data.

    Request Body:
        {
            "username": "user@example.com",
            "password": "secure_password"
        }

    Returns:
        {
            "access_token": "eyJ...",
            "token_type": "Bearer",
            "expires_in": 3600
        }

    Security:
        - Database users: bcrypt password hashing
        - Returns 401 if credentials are invalid or account is inactive
    """
    return await login_handler(login_request)


@router.post("/auth/s2s/token", response_model=S2STokenResponse)
async def generate_s2s_token(request: S2STokenRequest):
    """
    Generate long-lived token for Server-to-Server (S2S) authentication.

    This endpoint allows admin users to generate long-lived JWT tokens
    (up to 365 days) for automated integrations and S2S communication.

    Requirements:
    - Only admin users can generate S2S tokens (security restriction)
    - Maximum token lifetime: 365 days
    - Token should be stored securely by the caller

    Use Cases:
    - Backend service integrations
    - Automated scripts and cron jobs
    - Third-party platform integrations
    - CI/CD pipelines

    Request Body:
        {
            "username": "admin@example.com",
            "password": "secure_password",
            "token_lifetime_days": 90
        }

    Returns:
        {
            "success": true,
            "access_token": "eyJ...",
            "token_type": "Bearer",
            "expires_in": 7776000,
            "expires_at": "2026-03-20T12:00:00Z",
            "note": "Long-lived S2S token valid for 90 days. Store securely..."
        }

    Security Notes:
    - Store tokens in secure secret management systems (AWS Secrets Manager, HashiCorp Vault)
    - Rotate tokens before expiration
    - Never commit tokens to version control
    - Monitor token usage for suspicious activity
    - To revoke access, disable the user account

    Security:
        - Returns 401 if credentials are invalid or account is inactive
        - Returns 403 if user is not an admin
    """
    return await generate_s2s_token_handler(request)


@router.get("/auth/me", response_model=UserInfo)
async def get_current_user_info(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get current authenticated user information from JWT token.

    This endpoint extracts and returns user information from the Bearer token.
    Useful for:
    - Frontend to display user info
    - Checking current permissions
    - Verifying token validity
    - Getting accessible resellers and merchants

    Returns:
        {
            "id": "user-uuid",
            "username": "user@example.com",
            "role": "merchant",
            "email": "user@example.com",
            "reseller_ids": ["reseller_123"],
            "merchant_ids": ["merchant_456", "merchant_789"],
            "permissions": ["read:leads", "write:leads", ...]
        }

    Security:
        - Requires valid Bearer token in Authorization header
        - Returns 401 if token is invalid or expired
    """
    return await get_user_info_handler(current_user)


@router.post("/auth/logout")
async def logout_user():
    """
    Logout endpoint for JWT token-based authentication.

    Since JWT tokens are stateless and stored client-side:
    - Backend cannot invalidate the token (no session to destroy)
    - Client must delete the token from localStorage/cookies
    - Token will naturally expire after its lifetime

    This endpoint exists for:
    - API consistency (REST convention)
    - Future enhancements (e.g., token blacklisting)
    - Logging logout events

    Client-side logout steps:
    1. Call this endpoint (optional, for logging)
    2. Remove token from localStorage/cookies
    3. Redirect to login page
    4. Clear any user state in application

    Returns:
        {
            "success": true,
            "message": "Logout acknowledged. Client should clear token from storage.",
            "instructions": {
                "step_1": "Remove token from localStorage or cookies",
                "step_2": "Clear user state in your application",
                "step_3": "Redirect to login page",
                "note": "Token remains valid until expiration but client discards it"
            }
        }

    Note:
        The actual logout happens client-side by removing the token.
        The token remains technically valid until expiration, but the client
        discards it and can no longer use it.
    """
    return await logout_handler()


@router.get("/logout", include_in_schema=False)
async def logout():
    """
    Legacy logout endpoint for session-based authentication.

    [DEPRECATED] This endpoint is for backward compatibility with old session-based auth.
    New applications should use POST /auth/logout instead.

    This endpoint:
    - Deletes the session cookie
    - Redirects to login page
    - Clears browser cache
    """
    response = RedirectResponse(url="/agent/voice/breeze-buddy/login")
    response.delete_cookie("session")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
