"""
Business logic handlers for credential operations.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor import (
    create_credential,
    delete_credential,
    get_all_credentials,
    get_credential_by_id,
    get_credentials_by_merchant,
    update_credential,
)
from app.schemas import (
    Credential,
    CreateCredentialRequest,
    UpdateCredentialRequest,
    UserInfo,
)


async def create_credential_handler(
    req: CreateCredentialRequest, current_user: UserInfo
) -> Credential:
    """Create a new credential."""
    logger.info(
        f"User {current_user.username} creating credential '{req.name}' "
        f"for merchant: {req.merchant_id or 'GLOBAL'}"
    )

    try:
        credential = await create_credential(
            merchant_id=req.merchant_id,
            name=req.name,
            credential_type=req.credential_type,
            value=req.value,
            description=req.description,
        )

        if credential:
            logger.info(f"Credential '{req.name}' created: {credential.id}")
            return credential

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create credential. Name may already exist for this merchant.",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating credential: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create credential.",
        )


async def list_credentials_handler(
    merchant_id: Optional[str],
    current_user: UserInfo,
) -> List[Credential]:
    """List credentials with optional merchant filter."""
    logger.info(
        f"User {current_user.username} listing credentials "
        f"(merchant={merchant_id or 'all'})"
    )

    try:
        if merchant_id:
            return await get_credentials_by_merchant(merchant_id, mask=True)
        else:
            return await get_all_credentials(mask=True)

    except Exception as e:
        logger.error(f"Error listing credentials: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve credentials.",
        )


async def get_credential_handler(
    credential_id: str, current_user: UserInfo
) -> Credential:
    """Get a single credential by ID (masked value)."""
    logger.info(f"User {current_user.username} requesting credential: {credential_id}")

    try:
        credential = await get_credential_by_id(credential_id, mask=True)
        if not credential:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )
        return credential

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting credential {credential_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve credential.",
        )


async def update_credential_handler(
    credential_id: str,
    req: UpdateCredentialRequest,
    current_user: UserInfo,
) -> Credential:
    """Update a credential. Preserves masked values."""
    logger.info(f"User {current_user.username} updating credential: {credential_id}")

    try:
        existing = await get_credential_by_id(credential_id, mask=True)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )

        updated = await update_credential(
            credential_id=credential_id,
            name=req.name,
            credential_type=req.credential_type,
            value=req.value,
            description=req.description,
            is_active=req.is_active,
        )

        if updated:
            logger.info(f"Credential {credential_id} updated successfully")
            return updated

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to update credential.",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating credential {credential_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update credential.",
        )


async def delete_credential_handler(
    credential_id: str, current_user: UserInfo
) -> None:
    """Delete a credential."""
    logger.info(f"User {current_user.username} deleting credential: {credential_id}")

    try:
        existing = await get_credential_by_id(credential_id, mask=True)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )

        success = await delete_credential(credential_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )

        logger.info(f"Credential {credential_id} deleted successfully")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting credential {credential_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete credential.",
        )
