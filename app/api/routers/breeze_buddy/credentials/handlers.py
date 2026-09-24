"""
Business logic handlers for credential operations.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.accounts import SHAPES, shape_problems
from app.core.logger import logger
from app.database.accessor import (
    CredentialInUseError,
    create_credential,
    delete_credential,
    get_all_credentials,
    get_credential_by_id,
    get_credentials_by_merchant,
    update_credential,
)
from app.database.accessor.breeze_buddy.credentials import merge_credential_value
from app.database.decoder.breeze_buddy.credentials import CREDENTIAL_MASK
from app.schemas import (
    CreateCredentialRequest,
    Credential,
    UpdateCredentialRequest,
    UserInfo,
)


async def create_credential_handler(
    req: CreateCredentialRequest, current_user: UserInfo
) -> Credential:
    """Create a new credential."""
    logger.info(
        f"User {current_user.username} creating credential '{req.name}' "
        f"for reseller: {req.reseller_id or 'GLOBAL'}"
    )

    # A provider ACCOUNT row (LLM / STT / TTS) is judged here, at the write:
    # the provider word must be one the engine knows, and the value must
    # carry that provider's fields — never first discovered when a template
    # is saved, or on a call.
    _refuse_bad_account(req.provider, req.value)

    try:
        credential = await create_credential(
            reseller_id=req.reseller_id,
            merchant_id=req.merchant_id,
            name=req.name,
            credential_type=req.credential_type,
            value=req.value,
            description=req.description,
            provider=req.provider,
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
    reseller_id: Optional[str],
    current_user: UserInfo,
    merchant_id: Optional[str] = None,
    provider: Optional[str] = None,
) -> List[Credential]:
    """List credentials with optional reseller / merchant / provider filter.

    ``provider`` narrows to the rows a template's ``credential_id`` may name
    for that service — the console's account picker. The tenant read asks
    for that provider in SQL (its default reads placeholder rows only); the
    admin's unscoped read is filtered here."""
    logger.info(
        f"User {current_user.username} listing credentials "
        f"(reseller={reseller_id or 'all'} merchant={merchant_id or '-'} "
        f"provider={provider or '-'})"
    )

    try:
        if reseller_id:
            return await get_credentials_by_merchant(
                reseller_id, mask=True, merchant_id=merchant_id, provider=provider
            )
        rows = await get_all_credentials(mask=True)
        if provider:
            rows = [row for row in rows if row.provider == provider]
        return rows

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


def _refuse_bad_account(provider: Optional[str], value: Optional[dict]) -> None:
    """A provider account must name a known provider and carry its fields."""
    if not provider:
        return
    if provider not in SHAPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown provider {provider!r}; one of {', '.join(sorted(SHAPES))}",
        )
    lacking = shape_problems(provider, value or {})
    if lacking:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"provider_credentials": [f"{provider}: {p}" for p in lacking]},
        )


def _host_of(value: Optional[dict]) -> Optional[str]:
    return (value or {}).get("endpoint") or None


async def update_credential_handler(
    credential_id: str,
    req: UpdateCredentialRequest,
    current_user: UserInfo,
) -> Credential:
    """Update a credential. Preserves masked values."""
    logger.info(f"User {current_user.username} updating credential: {credential_id}")

    try:
        # Unmasked: the merged value is judged here, before the write.
        existing = await get_credential_by_id(credential_id, mask=False)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )

        provider = req.provider if req.provider is not None else existing.provider
        if provider and req.value is not None:
            # The edit as it will be stored: masked fields keep their value.
            merged = merge_credential_value(req.value, existing.value or {})
            _refuse_bad_account(provider, merged)
            # Whoever sets the host must hold the key (review, 24 Sep 2026):
            # an edit that sends the key masked keeps the stored key, so it
            # must not be able to point that key at a new host.
            if _host_of(merged) != _host_of(existing.value) and (
                req.value.get("api_key") in (None, CREDENTIAL_MASK)
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="changing an account's endpoint requires its full key",
                )
        elif req.provider is not None and req.provider != existing.provider:
            _refuse_bad_account(req.provider, existing.value)

        # The two edits that would strand every template naming this row —
        # switching it off, re-labelling its provider — are refused by the
        # UPDATE itself while one does (atomic, exact: see the query).
        stranding = req.is_active is False or (
            req.provider is not None and req.provider != existing.provider
        )
        updated = await update_credential(
            credential_id=credential_id,
            name=req.name,
            credential_type=req.credential_type,
            value=req.value,
            description=req.description,
            is_active=req.is_active,
            provider=req.provider,
            unless_named_by_a_template=stranding,
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
    except CredentialInUseError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Credential is named by a template as its provider account — "
            "point the template at another credential before deactivating "
            "or re-labelling this one.",
        )
    except Exception as e:
        logger.error(f"Error updating credential {credential_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update credential.",
        )


async def delete_credential_handler(credential_id: str, current_user: UserInfo) -> None:
    """Delete a credential."""
    # bind() rather than interpolation: credential_id lands as a structured
    # field a log search can filter on.
    log = logger.bind(credential_id=credential_id)
    log.info(f"User {current_user.username} deleting credential")

    try:
        existing = await get_credential_by_id(credential_id, mask=True)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )

        # A template that names this row as its provider account holds it
        # the way a connector FK does: the DELETE itself refuses while one
        # does (exact, atomic) and the accessor raises CredentialInUseError
        # — the 409 below.
        success = await delete_credential(credential_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {credential_id} not found",
            )

        log.info("Credential deleted successfully")

    except HTTPException:
        raise
    except CredentialInUseError as exc:
        # The credential exists and is still wired to something — a connector
        # installation holds it. 409, not 500: the request was understood and
        # deliberately refused, and the caller can act on it by disconnecting
        # the connector first. The accessor translated the driver's FK
        # violation, so this layer knows credentials, not Postgres.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Credential {credential_id} is in use — by a connector or as a "
                "template's provider account — and cannot be deleted. "
                "Disconnect the connector or re-point the template first."
            ),
        ) from exc
    except Exception as e:
        log.opt(exception=e).error("Error deleting credential")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete credential.",
        )
