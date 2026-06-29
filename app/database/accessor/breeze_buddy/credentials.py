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
    get_merchant_credential_by_name_query,
    insert_credential_query,
    update_credential_query,
    upsert_merchant_credential_by_name_query,
)
from app.schemas import Credential, CredentialType
from app.services.encryption import encrypt_credential


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


def _validate_credential_value(
    credential_type: CredentialType, value: Dict[str, Any]
) -> None:
    """
    Validate that credential value structure matches the credential_type.

    Raises:
        ValueError: If value structure doesn't match credential_type requirements
    """
    if credential_type == CredentialType.API_KEY:
        if "key" not in value:
            raise ValueError(
                "api_key credential type requires 'key' field in value. "
                "Expected format: {'key': 'your_api_key'}"
            )
        if not isinstance(value.get("key"), str) or not value.get("key"):
            raise ValueError("'key' field must be a non-empty string")

    elif credential_type == CredentialType.BEARER_TOKEN:
        if "token" not in value:
            raise ValueError(
                "bearer_token credential type requires 'token' field in value. "
                "Expected format: {'token': 'your_bearer_token'}"
            )
        if not isinstance(value.get("token"), str) or not value.get("token"):
            raise ValueError("'token' field must be a non-empty string")

    elif credential_type == CredentialType.BASIC_AUTH:
        if "username" not in value or "password" not in value:
            raise ValueError(
                "basic_auth credential type requires 'username' and 'password' fields in value. "
                "Expected format: {'username': '...', 'password': '...'}"
            )
        if not isinstance(value.get("username"), str) or not value.get("username"):
            raise ValueError("'username' field must be a non-empty string")
        if not isinstance(value.get("password"), str) or not value.get("password"):
            raise ValueError("'password' field must be a non-empty string")

    elif credential_type == CredentialType.CUSTOM:
        # Custom type allows any structure, but must have at least one key
        if not value:
            raise ValueError(
                "custom credential type requires at least one key-value pair in value"
            )


async def create_credential(
    reseller_id: Optional[str],
    name: str,
    credential_type: CredentialType,
    value: Dict[str, Any],
    description: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Optional[Credential]:
    """Create a new credential with optional KMS encryption."""
    logger.info(
        f"Creating credential '{name}' for reseller: {reseller_id or 'GLOBAL'}, "
        f"merchant: {merchant_id or 'NONE'}"
    )

    try:
        # Validate value structure matches credential_type
        _validate_credential_value(credential_type, value)

        stored_value, is_encrypted = encrypt_credential(value)

        query_text, values = insert_credential_query(
            id=str(uuid4()),
            reseller_id=reseller_id,
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
            logger.info(
                f"Credential '{name}' created successfully (encrypted={is_encrypted})"
            )
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


async def get_merchant_credential_by_name(
    reseller_id: Optional[str],
    merchant_id: str,
    name: str,
    mask: bool = True,
) -> Optional[Credential]:
    """Get an active named credential scoped to one merchant."""

    try:
        query_text, values = get_merchant_credential_by_name_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            name=name,
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_credential(result, mask=mask)
    except Exception as e:
        logger.error(
            f"Error getting credential '{name}' for merchant {merchant_id}: {e}"
        )
        return None


async def upsert_merchant_credential_by_name(
    reseller_id: Optional[str],
    merchant_id: str,
    name: str,
    credential_type: CredentialType,
    value: Dict[str, Any],
    description: Optional[str] = None,
    mask: bool = True,
) -> Optional[Credential]:
    """Create or update a named merchant-scoped credential."""

    try:
        _validate_credential_value(credential_type, value)
        stored_value, is_encrypted = encrypt_credential(value)
        query_text, values = upsert_merchant_credential_by_name_query(
            id=str(uuid4()),
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            name=name,
            credential_type=credential_type.value,
            value=stored_value,
            is_encrypted=is_encrypted,
            description=description,
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_credential(result, mask=mask)
    except Exception as e:
        logger.error(
            f"Error upserting credential '{name}' for merchant {merchant_id}: {e}",
            exc_info=True,
        )
        return None


async def get_credentials_by_merchant(
    reseller_id: Optional[str],
    mask: bool = True,
) -> List[Credential]:
    """
    Get global and reseller-scoped credentials for template/API configuration.
    Merchant-scoped system credentials are fetched through explicit helpers.
    """
    try:
        query_text, values = get_credentials_by_merchant_query(reseller_id)
        result = await run_parameterized_query(query_text, values)
        return decode_credential_list(result, mask=mask)
    except Exception as e:
        logger.error(f"Error getting credentials for merchant {reseller_id}: {e}")
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
    reseller_id: str,
) -> Dict[str, Any]:
    """
    Get credentials as a flat dict for template_vars resolution.
    Merges global + reseller-specific credentials.
    """
    try:
        query_text, values = get_credentials_by_merchant_query(reseller_id)
        result = await run_parameterized_query(query_text, values)
        return decode_credentials_as_dict(result)
    except Exception as e:
        logger.error(
            f"Error getting credentials as template vars for merchant {reseller_id}: {e}"
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

        # Get existing credential for merging and validation
        existing = await get_credential_by_id(credential_id, mask=False)
        if not existing:
            logger.error(f"Credential {credential_id} not found")
            return None

        if value is not None:
            # Merge masked values with existing
            if existing.value:
                merged_value = _merge_credential_value(value, existing.value)
            else:
                merged_value = value

            # Use new credential_type for validation (required when updating value)
            if credential_type is None:
                raise ValueError("credential_type is required when updating value")

            # Validate merged value structure matches credential_type
            _validate_credential_value(credential_type, merged_value)

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

    except ValueError:
        # Re-raise validation errors
        raise
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
