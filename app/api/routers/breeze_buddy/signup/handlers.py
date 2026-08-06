"""
Self-service merchant signup handlers.

Business logic for three public signup/SSO flows:

1. signup_with_password_handler
   - Validates input
   - Checks merchant_id + username uniqueness
   - Creates merchant entity in `merchants` table (reseller_id =
     the self-serve umbrella, see SELF_SIGNUP_RESELLER_ID)
   - Creates user login account in `users` table (role = merchant, owner_id = None)
   - Returns JWT

2. google_login_handler
   - Verifies Google id_token with Google's public keys
   - Looks up existing user by Google email
   - If found  → returns JWT (standard login)
   - If not    → raises HTTP 404 with detail="no_account" (frontend shows step-2 form)

3. google_signup_handler
   - Re-verifies the Google id_token (same token from step 1)
   - Checks merchant_id uniqueness
   - Derives a username from the Google email (e.g. "alice" from "alice@gmail.com")
   - Creates merchant entity + user login account
   - Returns JWT

Security invariants (enforced here, never trusted from client):
  - role is always UserRole.MERCHANT
  - reseller_id is always the self-serve umbrella (SELF_SIGNUP_RESELLER_ID,
    default "breeze-self-serve" — env-overridable) so self-registered
    merchants are distinguishable from manually onboarded ones (breeze)
  - merchant_ids is always [merchant_id] (the just-created merchant)
  - reseller_ids is always [SELF_SIGNUP_RESELLER_ID]
  - owner_id is always None (self-registered → no parent user)
  - No admin / reseller role is ever assigned by these handlers
"""

import asyncio
import re
import secrets
import string
from typing import Any, Dict

import asyncpg
from fastapi import HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.core.config.static import (
    GOOGLE_CLIENT_ID,
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    SELF_SIGNUP_RESELLER_ID,
)
from app.core.logger import logger
from app.core.security.password import verify_password_async
from app.core.security.scope import resolve_merchant_ids, resolve_reseller_ids
from app.database.accessor.breeze_buddy import (
    merchants as merchant_accessors,
    users as user_accessors,
)
from app.schemas import TokenResponse, UserInfo, UserRole
from app.schemas.breeze_buddy.signup import (
    AccountSummary,
    GoogleAuthRequest,
    GoogleMerchantSignupRequest,
    MerchantSignupRequest,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _verify_google_id_token_blocking(id_token: str) -> Dict[str, Any]:
    """
    Verify a Google id_token and return its claims.

    Uses google-auth's `id_token.verify_oauth2_token` which fetches
    Google's public keys and validates the JWT signature, audience, and
    expiry.

    Args:
        id_token: The raw id_token string from the GSI SDK callback.

    Returns:
        Decoded payload dict containing at least `email`, `sub`, `name`.

    Raises:
        HTTPException 401: if the token is invalid, expired, or the
            audience does not match GOOGLE_CLIENT_ID.
        HTTPException 503: if GOOGLE_CLIENT_ID is not configured.
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google SSO is not configured on this server.",
        )

    try:
        request = google_requests.Request()
        claims = google_id_token.verify_oauth2_token(
            id_token, request, GOOGLE_CLIENT_ID
        )
        if not claims.get("email_verified"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google account email is not verified. Please verify your Google email and try again.",
            )
        return dict(claims)
    except ValueError as exc:
        logger.warning(f"Google id_token verification failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Google token. Please sign in again.",
        )


async def _verify_google_id_token(id_token: str) -> Dict[str, Any]:
    """
    Verify a Google id_token off the event loop.

    ``verify_oauth2_token`` fetches Google's public signing certificates over
    HTTPS with a synchronous client on cache miss (100-800ms). On a single-
    worker pod that freezes every in-flight call and webhook for that window.
    """
    return await asyncio.to_thread(_verify_google_id_token_blocking, id_token)


def _derive_username_from_email(email: str) -> str:
    """
    Derive a username from an email address.

    Example: "alice.smith@gmail.com" → "alice.smith"
    Spaces and invalid characters are removed.
    """
    local = email.split("@")[0]
    # Keep only alphanumeric, hyphens, underscores, dots
    clean = re.sub(r"[^a-zA-Z0-9_.\-]", "", local)
    return clean or "user"


def _random_suffix(length: int = 6) -> str:
    """Generate a short random alphanumeric suffix for disambiguation."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _build_and_return_token(user_id: str, user_record) -> TokenResponse:
    """
    Resolve scopes and mint a JWT for the given user_id / UserInDB record.
    Reuses the same logic as the login handler.
    """
    resolved_reseller_ids = user_record.reseller_ids
    resolved_merchant_ids = user_record.merchant_ids

    if user_record.role != UserRole.ADMIN:
        temp_user_info = UserInfo(
            id=user_record.id,
            username=user_record.username,
            role=user_record.role,
            email=user_record.email,
            reseller_ids=user_record.reseller_ids,
            merchant_ids=user_record.merchant_ids,
            permissions=[],
            owner_id=user_record.owner_id,
        )
        resolved_r = await resolve_reseller_ids(temp_user_info)
        if resolved_r is not None:
            resolved_reseller_ids = resolved_r

        resolved_m = await resolve_merchant_ids(temp_user_info)
        if resolved_m is not None:
            resolved_merchant_ids = resolved_m

    access_token = rbac_token_manager.create_access_token_with_rbac(
        user_id=user_record.id,
        username=user_record.username,
        role=user_record.role,
        reseller_ids=resolved_reseller_ids,
        merchant_ids=resolved_merchant_ids,
        email=user_record.email,
        owner_id=user_record.owner_id,
    )

    return TokenResponse(
        access_token=access_token,
        token_type="Bearer",
        expires_in=JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Handler 1 — Username / password signup
# ─────────────────────────────────────────────────────────────────────────────


async def signup_with_password_handler(
    request: MerchantSignupRequest,
) -> TokenResponse:
    """
    Create a new merchant entity + login account via username/password.

    Steps:
    1. Validate that merchant_id is available.
    2. Validate that username is available.
    3. Validate that email is available (if provided).
    4. Create the merchant entity (reseller_id = SELF_SIGNUP_RESELLER_ID).
    5. Create the user account (role = merchant, merchant_ids = [merchant_id]).
    6. Mint and return a JWT.
    """
    # 1. Check merchant_id uniqueness
    merchant_exists = await merchant_accessors.check_merchant_identifier_exists(
        request.merchant_id
    )
    if merchant_exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Merchant ID '{request.merchant_id}' is already taken. Please choose another.",
        )

    # 2. Check username uniqueness
    username_exists = await user_accessors.check_username_exists(request.username)
    if username_exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{request.username}' is already taken. Please choose another.",
        )

    # 3. Check email uniqueness (if provided)
    if request.email:
        email_exists = await user_accessors.check_email_exists(request.email)
        if email_exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An account with email '{request.email}' already exists. Try signing in instead.",
            )

    # 4+5. Create merchant + user atomically in one transaction
    try:
        await user_accessors.create_merchant_and_user_atomically(
            merchant_id=request.merchant_id,
            merchant_name=request.name,
            merchant_description=request.description,
            reseller_id=SELF_SIGNUP_RESELLER_ID,
            user_id=request.merchant_id,
            username=request.username,
            password=request.password,
            email=request.email,
            role=UserRole.MERCHANT,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Merchant ID or username already exists.",
        )

    logger.info(
        f"Self-service signup: merchant='{request.merchant_id}' "
        f"user='{request.username}' reseller='{SELF_SIGNUP_RESELLER_ID}'"
    )

    # 5. Fetch the full UserInDB to build the token (create_user returns UserResponse)
    user_in_db = await user_accessors.get_user_by_username(request.username)
    if not user_in_db:
        raise HTTPException(
            status_code=500, detail="Signup succeeded but could not retrieve account."
        )

    return await _build_and_return_token(user_in_db.id, user_in_db)


# ─────────────────────────────────────────────────────────────────────────────
# Handler 2 — Google SSO login (existing user)
# ─────────────────────────────────────────────────────────────────────────────


async def google_login_handler(request: GoogleAuthRequest) -> TokenResponse:
    """
    Authenticate an existing user via Google id_token.

    Verifies the token, extracts the email, and looks up a matching user
    account. If found, returns a JWT. If not found, raises HTTP 404 with
    `detail="no_account"` so the frontend can show the signup form.
    """
    claims = await _verify_google_id_token(request.id_token)
    google_email: str = claims.get("email", "")

    if not google_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account does not have an email address.",
        )

    user = await user_accessors.get_user_by_email(google_email)
    if not user:
        # Signal to the frontend to show the signup step
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_account",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive. Please contact support.",
        )

    logger.info(f"Google SSO login: user='{user.username}' email='{google_email}'")
    return await _build_and_return_token(user.id, user)


# ─────────────────────────────────────────────────────────────────────────────
# Handler 3 — Google SSO merchant signup (new user)
# ─────────────────────────────────────────────────────────────────────────────


async def google_signup_handler(request: GoogleMerchantSignupRequest) -> TokenResponse:
    """
    Register a new merchant via Google SSO (step 2 after google_login_handler 404).

    Re-verifies the id_token, then creates:
    - A merchant entity with reseller_id = SELF_SIGNUP_RESELLER_ID
    - A user account with role = merchant, email = Google email, no password
    """
    claims = await _verify_google_id_token(request.id_token)
    google_email: str = claims.get("email", "")

    if not google_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account does not have an email address.",
        )

    # Ensure no existing account already has this email
    existing = await user_accessors.get_user_by_email(google_email)
    if existing:
        if not existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is inactive. Please contact support.",
            )
        logger.info(
            f"Google SSO signup called for existing user '{existing.username}' — returning token"
        )
        return await _build_and_return_token(existing.id, existing)

    # Check merchant_id uniqueness
    merchant_exists = await merchant_accessors.check_merchant_identifier_exists(
        request.merchant_id
    )
    if merchant_exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Merchant ID '{request.merchant_id}' is already taken. Please choose another.",
        )

    # Derive a unique username from the Google email
    base_username = _derive_username_from_email(google_email)
    username = base_username
    for _ in range(5):
        if not await user_accessors.check_username_exists(username):
            break
        username = f"{base_username}_{_random_suffix()}"
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not derive a unique username. Please try again.",
        )

    # Create merchant + user atomically in one transaction
    random_password = secrets.token_urlsafe(32)
    try:
        await user_accessors.create_merchant_and_user_atomically(
            merchant_id=request.merchant_id,
            merchant_name=request.name,
            merchant_description=request.description,
            reseller_id=SELF_SIGNUP_RESELLER_ID,
            user_id=request.merchant_id,
            username=username,
            password=random_password,
            email=google_email,
            role=UserRole.MERCHANT,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Merchant ID or username already exists.",
        )

    logger.info(
        f"Google SSO signup: merchant='{request.merchant_id}' "
        f"user='{username}' email='{google_email}' "
        f"reseller='{SELF_SIGNUP_RESELLER_ID}'"
    )

    user_in_db = await user_accessors.get_user_by_username(username)
    if not user_in_db:
        raise HTTPException(
            status_code=500, detail="Signup succeeded but could not retrieve account."
        )

    return await _build_and_return_token(user_in_db.id, user_in_db)


# ─────────────────────────────────────────────────────────────────────────────
# Handler 4 — List all accounts for an email (account picker)
# ─────────────────────────────────────────────────────────────────────────────


async def list_accounts_handler(
    id_token: str | None,
    email: str | None,
) -> list:
    """
    Return all user accounts that share a given email address.

    Called immediately after Google OAuth or password auth succeeds to
    give the frontend a list of accounts the user can pick from.

    For Google flows: pass `id_token` — the backend verifies it and
    extracts the email.
    For password flows: pass `email` directly (already verified by the
    login handler upstream, so no re-auth needed here).
    """
    if id_token:
        claims = await _verify_google_id_token(id_token)
        resolved_email = claims.get("email", "")
        if not resolved_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google account does not have an email address.",
            )
    elif email:
        resolved_email = email
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Either id_token or email must be provided.",
        )

    users = await user_accessors.get_users_by_email(resolved_email)

    return [
        AccountSummary(
            id=u.id,
            username=u.username,
            role=u.role.value,
            email=u.email,
            merchant_ids=u.merchant_ids,
            reseller_ids=u.reseller_ids,
            is_active=u.is_active,
        )
        for u in users
        if u.is_active
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Handler 5 — Select a specific account and mint JWT
# ─────────────────────────────────────────────────────────────────────────────


async def select_account_handler(
    account_id: str,
    id_token: str | None,
    password: str | None,
) -> "TokenResponse":
    """
    Mint a JWT for a specific account after the user picks from the list.

    Verifies identity one more time before minting:
    - Google flow: re-verify id_token, confirm the account's email matches.
    - Password flow: verify the password against the account's stored hash.
    """
    # Load the target account
    target = await user_accessors.get_user_in_db_by_id(account_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found.",
        )
    if not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive. Please contact support.",
        )

    if id_token:
        # Google SSO: verify token and confirm email ownership
        claims = await _verify_google_id_token(id_token)
        google_email = claims.get("email", "")
        if not google_email or google_email != target.email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google account does not match the selected account.",
            )
    elif password:
        # Password: verify against stored hash
        if not await verify_password_async(password, target.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect password.",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Either id_token or password must be provided.",
        )

    logger.info(
        f"Account selected: id='{account_id}' role='{target.role.value}' "
        f"username='{target.username}'"
    )
    return await _build_and_return_token(target.id, target)


# ─────────────────────────────────────────────────────────────────────────────
# Handler 6 — Switch account (authenticated)
# ─────────────────────────────────────────────────────────────────────────────


async def switch_account_handler(
    account_id: str,
    current_user: "UserInfo",
) -> "TokenResponse":
    """
    Switch to a different account while already authenticated.

    The caller must hold a valid JWT (current_user injected by auth dependency).
    We verify the target account shares the same email as the current user so
    a user cannot hijack an unrelated account.  No password/token re-verification
    is required because the existing JWT already proves identity.
    """
    target = await user_accessors.get_user_in_db_by_id(account_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found.",
        )
    if not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive. Please contact support.",
        )

    # Security: target must share the same email as the caller
    if not target.email or target.email != current_user.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only switch to accounts linked to your email address.",
        )

    logger.info(
        f"Account switch: from='{current_user.username}' to='{target.username}' "
        f"role='{target.role.value}'"
    )
    return await _build_and_return_token(target.id, target)
