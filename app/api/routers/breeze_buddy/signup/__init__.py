"""
Public self-service signup and Google SSO endpoints.

All endpoints are **unauthenticated** (no Bearer token required) except
/auth/switch-account which requires a valid JWT.

Endpoints:
  POST /signup              — DISABLED: username/password merchant registration
  POST /auth/google         — Google SSO login (existing user → JWT)
  POST /signup/google       — DISABLED: Google SSO merchant registration (new user → JWT)
  POST /auth/accounts       — list accounts for an email (account picker)
  POST /auth/select-account — select account and mint JWT
  POST /auth/switch-account — switch to sibling account (authenticated)
"""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import TokenResponse, UserInfo
from app.schemas.breeze_buddy.signup import (
    AccountsResponse,
    GoogleAuthRequest,
    ListAccountsRequest,
    SelectAccountRequest,
    SwitchAccountRequest,
)

from .handlers import (
    google_login_handler,
    list_accounts_handler,
    select_account_handler,
    switch_account_handler,
)

# Self-service signup is disabled. To re-enable it, restore these imports and
# uncomment the /signup and /signup/google routes below:
# from app.schemas.breeze_buddy.signup import (
#     GoogleMerchantSignupRequest,
#     MerchantSignupRequest,
# )
# from .handlers import google_signup_handler, signup_with_password_handler

router = APIRouter()


# Self-service signup is disabled: new merchants are onboarded by us, not by a
# public form. Uncomment this route (and its imports above) to re-enable it.
# @router.post(
#     "/signup",
#     response_model=TokenResponse,
#     status_code=201,
#     summary="Merchant self-service registration (username/password)",
#     tags=["signup"],
# )
# async def signup_with_password(request: MerchantSignupRequest) -> TokenResponse:
#     return await signup_with_password_handler(request)


@router.post(
    "/auth/google",
    response_model=TokenResponse,
    summary="Google SSO login (existing user)",
    tags=["signup"],
)
async def google_login(request: GoogleAuthRequest) -> TokenResponse:
    return await google_login_handler(request)


# Self-service signup is disabled (see /signup above). Uncomment this route
# (and its imports above) to re-enable Google signup.
# @router.post(
#     "/signup/google",
#     response_model=TokenResponse,
#     status_code=201,
#     summary="Google SSO merchant registration (new user)",
#     tags=["signup"],
# )
# async def google_signup(request: GoogleMerchantSignupRequest) -> TokenResponse:
#     return await google_signup_handler(request)


@router.post(
    "/auth/accounts",
    response_model=AccountsResponse,
    summary="List all accounts for an email (account picker)",
    tags=["signup"],
)
async def list_accounts(request: ListAccountsRequest) -> AccountsResponse:
    accounts = await list_accounts_handler(
        id_token=request.id_token, email=request.email
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
