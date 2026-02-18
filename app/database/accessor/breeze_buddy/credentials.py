"""
Database accessor functions for the credentials table.
"""

from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.core.logger import logger
from app.database.decoder.breeze_buddy.credentials import (
    CREDENTIAL_MASK,
    decode_credential_list,
    decode_credentials_as_dict,
    decode_single_credential,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.credentials import (
    delete_credential_query,
    get_all_credentials_query,
    get_credential_by_id_query,
    get_credentials_by_merchant_query,
    insert_credential_query,
    update_credential_query,
)
from app.schemas import Credential, CredentialType
from app.services.gcp.kms import decrypt_credential, encrypt_credential


def _merge_credential_value(
    incoming: Dict[str, Any],
    existing: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Merge incoming credential value with existing, preserving masked fields.
    Same pattern as template secrets merge.
    """
    merged = {}
    for key, value in incoming.items():
        if value == CREDENTIAL_MASK:
            if key in existing:
                merged[key] = existing[key]
        else:
            merged[key] = value
    return merged


async def create_credential(
    merchant_id: Optional[str],
    name: str,
    credential_type: CredentialType,
    value: Dict[str, Any],
    description: Optional[str] = None,
) -> Optional[Credential]:
    """Create a new credential with optional KMS encryption."""
    logger.info(
        f"Creating credential '{name}' for merchant: {merchant_id or 'GLOBAL'}"
    )

    try:
        stored_value, is_encrypted = encrypt_credential(value)

        query_text, values = insert_credential_query(
            id=str(uuid4()),
            merchant_id=merchant_id,
            name=name,
            credential_type=credential_type.value,
            value=stored_value,
            is_encrypted=is_encrypted,
            description=description,
        )

        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            credential = decode_single_credential(result, mask=True)
            logger.info(f"Credential '{name}' created successfully (encrypted={is_encrypted})")
            return credential

        logger.error(f"Failed to create credential '{name}'")
        return None

    except Exception as e:
        logger.error(f"Error creating credential '{name}': {e}", exc_info=True)
        return None


async def get_credential_by_id(
    credential_id: str,
    mask: bool = True,
) -> Optional[Credential]:
    """Get a credential by ID."""
    try:
        query_text, values = get_credential_by_id_query(credential_id)
        result = await run_parameterized_query(query_text, values)
        return decode_single_credential(result, mask=mask)
    except Exception as e:
        logger.error(f"Error getting credential by ID: {e}")
        return None


async def get_credentials_by_merchant(
    merchant_id: Optional[str],
    mask: bool = True,
) -> List[Credential]:
    """
    Get credentials for a merchant (includes global credentials).
    Results ordered: global first, then merchant-specific.
    """
    try:
        query_text, values = get_credentials_by_merchant_query(merchant_id)
        result = await run_parameterized_query(query_text, values)
        return decode_credential_list(result, mask=mask)
    except Exception as e:
        logger.error(f"Error getting credentials for merchant {merchant_id}: {e}")
        return []


async def get_all_credentials(mask: bool = True) -> List[Credential]:
    """Get all credentials (admin use)."""
    try:
        query_text, values = get_all_credentials_query()
        result = await run_parameterized_query(query_text, values)
        return decode_credential_list(result, mask=mask)
    except Exception as e:
        logger.error(f"Error getting all credentials: {e}")
        return []


async def get_credentials_as_template_vars(
    merchant_id: str,
) -> Dict[str, Any]:
    """
    Get credentials as a flat dict for template_vars resolution.
    Merges global + merchant-specific credentials (merchant overrides global).
    """
    try:
        query_text, values = get_credentials_by_merchant_query(merchant_id)
        result = await run_parameterized_query(query_text, values)
        return decode_credentials_as_dict(result)
    except Exception as e:
        logger.error(
            f"Error getting credentials as template vars for merchant {merchant_id}: {e}"
        )
        return {}


async def update_credential(
    credential_id: str,
    name: Optional[str] = None,
    credential_type: Optional[CredentialType] = None,
    value: Optional[Dict[str, Any]] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[Credential]:
    """
    Update a credential. Handles value encryption and masked field preservation.
    """
    logger.info(f"Updating credential {credential_id}")

    try:
        stored_value = None
        is_encrypted = None

        if value is not None:
            # Get existing credential to merge masked values
            existing = await get_credential_by_id(credential_id, mask=False)
            if existing and existing.value:
                merged_value = _merge_credential_value(value, existing.value)
            else:
                merged_value = value

            stored_value, is_encrypted = encrypt_credential(merged_value)

        query_text, values = update_credential_query(
            credential_id=credential_id,
            name=name,
            credential_type=credential_type.value if credential_type else None,
            value=stored_value,
            is_encrypted=is_encrypted,
            description=description,
            is_active=is_active,
        )

        result = await run_parameterized_query(query_text, values)
        if result and len(result) > 0:
            credential = decode_single_credential(result, mask=True)
            logger.info(f"Credential {credential_id} updated successfully")
            return credential

        logger.error(f"Failed to update credential {credential_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating credential {credential_id}: {e}", exc_info=True)
        return None


async def delete_credential(credential_id: str) -> bool:
    """Delete a credential by ID."""
    logger.info(f"Deleting credential {credential_id}")

    try:
        query_text, values = delete_credential_query(credential_id)
        result = await run_parameterized_query(query_text, values)

        if result and len(result) > 0:
            logger.info(f"Credential {credential_id} deleted successfully")
            return True

        logger.warning(f"Credential {credential_id} not found for deletion")
        return False

    except Exception as e:
        logger.error(f"Error deleting credential {credential_id}: {e}")
        return False
