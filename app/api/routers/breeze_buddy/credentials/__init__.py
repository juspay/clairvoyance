"""
Credentials management endpoints with RBAC.

Provides REST API for managing centralized credentials (API keys, tokens, etc.)
that are available as {placeholder} variables in templates, hooks, and pre-checks.

Endpoints:
- POST   /credentials           - Create new credential
- GET    /credentials           - List credentials (with optional merchant filter)
- GET    /credentials/{id}      - Get single credential (masked value)
- PUT    /credentials/{id}      - Update credential
- DELETE /credentials/{id}      - Delete credential
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import (
    CreateCredentialRequest,
    Credential,
    UpdateCredentialRequest,
    UserInfo,
)

from .handlers import (
    create_credential_handler,
    delete_credential_handler,
    get_credential_handler,
    list_credentials_handler,
    update_credential_handler,
)

router = APIRouter()


@router.post(
    "/credentials",
    response_model=Credential,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential_endpoint(
    req: CreateCredentialRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a new credential.

    - Global credentials (merchant_id=null): available to all merchants as {name} placeholder
    - Merchant credentials (merchant_id set): available only to that merchant's templates

    Values are encrypted at rest when CREDENTIAL_ENCRYPTION_KEY is configured.
    """
    # Only admins can create global credentials
    if req.merchant_id is None and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create global credentials",
        )

    # Non-admin: validate merchant access
    if req.merchant_id and current_user.role != "admin":
        if (
            req.merchant_id not in current_user.merchant_ids
            and "*" not in current_user.merchant_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {req.merchant_id}",
            )

    return await create_credential_handler(req, current_user)


@router.get("/credentials", response_model=List[Credential])
async def list_credentials_endpoint(
    merchant_id: Optional[str] = Query(
        None, description="Filter by merchant ID. Omit to list all (admin only)."
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List credentials with optional merchant filter.

    - Admin: sees all credentials or filtered by merchant
    - Merchant: must provide merchant_id, sees merchant + global credentials
    - Values are always masked in responses
    """
    # Non-admin must provide merchant_id
    if not merchant_id and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Non-admin users must provide a merchant_id filter",
        )

    # Validate merchant access
    if merchant_id and current_user.role != "admin":
        if (
            merchant_id not in current_user.merchant_ids
            and "*" not in current_user.merchant_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {merchant_id}",
            )

    return await list_credentials_handler(merchant_id, current_user)


@router.get("/credentials/{credential_id}", response_model=Credential)
async def get_credential_endpoint(
    credential_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get a single credential by ID. Values are masked in the response.

    - Admin: can view any credential
    - Merchant user: can only view global credentials or their own merchant's credentials
    """
    # Fetch credential first to check authorization
    credential = await get_credential_handler(credential_id, current_user)

    # RBAC check: non-admin users can only access global or their own merchant's credentials
    if current_user.role != "admin":
        if credential.merchant_id is not None:
            if (
                credential.merchant_id not in current_user.merchant_ids
                and "*" not in current_user.merchant_ids
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to credential for merchant {credential.merchant_id}",
                )

    return credential


@router.put("/credentials/{credential_id}", response_model=Credential)
async def update_credential_endpoint(
    credential_id: str,
    req: UpdateCredentialRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update a credential.

    - Send '******' for value fields you want to keep unchanged
    - New values will be re-encrypted if CREDENTIAL_ENCRYPTION_KEY is configured
    - Admin: can update any credential
    - Merchant user: can only update their own merchant's credentials (not global)
    """
    # Fetch credential first to check authorization
    credential = await get_credential_handler(credential_id, current_user)

    # RBAC check: non-admin users cannot update global credentials
    if current_user.role != "admin":
        if credential.merchant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin users can update global credentials",
            )
        # Check merchant access
        if (
            credential.merchant_id not in current_user.merchant_ids
            and "*" not in current_user.merchant_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to credential for merchant {credential.merchant_id}",
            )

    return await update_credential_handler(credential_id, req, current_user)


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential_endpoint(
    credential_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete a credential. Admin only.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can delete credentials",
        )

    await delete_credential_handler(credential_id, current_user)
    return None
