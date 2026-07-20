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

from fastapi import APIRouter, Depends, Request

from app.api.routers.breeze_buddy.auth.rate_limit import enforce_credential_rate_limit
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
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
    google_login_handler,
    google_signup_handler,
    list_accounts_handler,
    select_account_handler,
    signup_with_password_handler,
    switch_account_handler,
)

router = APIRouter()


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
    await enforce_credential_rate_limit(http_request, request.username)
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
    request: ListAccountsRequest, http_request: Request
) -> AccountsResponse:
    await enforce_credential_rate_limit(http_request, request.email)
    accounts = await list_accounts_handler(
        id_token=request.id_token, email=request.email, password=request.password
    )
    return AccountsResponse(accounts=accounts)


@router.post(
    "/auth/select-account",
    response_model=TokenResponse,
    summary="Select an account and mint JWT",
    tags=["signup"],
)
async def select_account(request: SelectAccountRequest) -> TokenResponse:
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
