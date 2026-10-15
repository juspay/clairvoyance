"""
Public self-service signup and Google SSO endpoints.

All endpoints are **unauthenticated** (no Bearer token required) except
/auth/switch-account which requires a valid JWT.

Endpoints:
  POST /signup              — username/password merchant registration
  POST /auth/google         — Google SSO login (existing user → JWT)
  POST /signup/google       — Google SSO merchant registration (new user → JWT)
  POST /auth/accounts       — list accounts for an email (account picker)
  POST /auth/select-account — select account and mint JWT
  POST /auth/switch-account — switch to sibling account (authenticated)
"""

from contextlib import nullcontext
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.routers.breeze_buddy.auth.rate_limit import credential_attempt
from app.api.security.breeze_buddy.rbac_token import (
    get_current_user_with_rbac,
    rbac_token_manager,
)
from app.schemas import TokenResponse, UserInfo
from app.schemas.breeze_buddy.signup import (
    AccountsResponse,
    GoogleAuthRequest,
    GoogleMerchantSignupRequest,
    ListAccountsRequest,
    MerchantSignupRequest,
    SelectAccountRequest,
    SwitchAccountRequest,
)

from .handlers import (
    _same_email,
    google_login_handler,
    google_signup_handler,
    list_accounts_handler,
    select_account_handler,
    signup_with_password_handler,
    switch_account_handler,
)

router = APIRouter()

_optional_bearer = HTTPBearer(auto_error=False)


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=201,
    summary="Merchant self-service registration (username/password)",
    tags=["signup"],
)
async def signup_with_password(
    request: MerchantSignupRequest, http_request: Request
) -> TokenResponse:
    async with credential_attempt(http_request, request.username):
        return await signup_with_password_handler(request)


@router.post(
    "/auth/google",
    response_model=TokenResponse,
    summary="Google SSO login (existing user)",
    tags=["signup"],
)
async def google_login(request: GoogleAuthRequest) -> TokenResponse:
    return await google_login_handler(request)


@router.post(
    "/signup/google",
    response_model=TokenResponse,
    status_code=201,
    summary="Google SSO merchant registration (new user)",
    tags=["signup"],
)
async def google_signup(request: GoogleMerchantSignupRequest) -> TokenResponse:
    return await google_signup_handler(request)


@router.post(
    "/auth/accounts",
    response_model=AccountsResponse,
    summary="List all accounts for an email (account picker)",
    tags=["signup"],
)
async def list_accounts(
    request: ListAccountsRequest,
    http_request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> AccountsResponse:
    # A signed-in caller listing its own email (the console's account switcher)
    # proves ownership with its token. Anyone else must present a password or a
    # Google id_token (PT-16). Only the password branch is a guess worth capping:
    # Google verifies an id_token, and Google sign-in is most of this route's
    # traffic — counting it would lock a shared office IP out of sign-in.
    # The token counts as proof only for its own email; any other email is a
    # password guess and takes the cap. A stale token (the console attaches
    # whatever it holds) falls back to the password path rather than 401.
    caller_email = None
    if credentials and request.email and not request.id_token:
        try:
            caller = await rbac_token_manager.verify_rbac_token(credentials.credentials)
        except HTTPException:
            caller = None
        if caller and caller.email and _same_email(caller.email, request.email):
            caller_email = caller.email
    guess = caller_email is None and not request.id_token and request.password
    async with (
        credential_attempt(http_request, request.email) if guess else nullcontext()
    ):
        accounts = await list_accounts_handler(
            id_token=request.id_token,
            email=request.email,
            password=request.password,
            caller_email=caller_email,
        )
    return AccountsResponse(accounts=accounts)


@router.post(
    "/auth/select-account",
    response_model=TokenResponse,
    summary="Select an account and mint JWT",
    tags=["signup"],
)
async def select_account(
    request: SelectAccountRequest, http_request: Request
) -> TokenResponse:
    # This endpoint verifies a password too, so it needs the same cap as
    # /auth/accounts and /auth/login. Without it an attacker who knows an
    # account_id can guess passwords here at full speed and never touch a
    # rate-limited route (PT-16). Bucketed on the account being targeted. The
    # Google branch (id_token, checked first by the handler) is not a guess.
    guess = not request.id_token and request.password
    async with (
        credential_attempt(http_request, request.account_id) if guess else nullcontext()
    ):
        return await select_account_handler(
            account_id=request.account_id,
            id_token=request.id_token,
            password=request.password,
        )


@router.post(
    "/auth/switch-account",
    response_model=TokenResponse,
    summary="Switch to a sibling account (authenticated)",
    tags=["signup"],
)
async def switch_account(
    request: SwitchAccountRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TokenResponse:
    return await switch_account_handler(
        account_id=request.account_id,
        current_user=current_user,
    )
