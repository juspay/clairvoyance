"""
Decoder functions for credentials table.
"""

from typing import Any, Dict, List, Optional

import asyncpg

from app.core.logger import logger
from app.schemas import Credential, CredentialType
from app.services.encryption import decrypt_credential

# Mask value for API responses
CREDENTIAL_MASK = "******"


def _decrypt_and_parse_value(
    stored_value: str, is_encrypted: bool
) -> Optional[Dict[str, Any]]:
    """Decrypt and parse the stored credential value."""
    result = decrypt_credential(stored_value, is_encrypted)
    if result is None:
        logger.error("Failed to decrypt/parse credential value")
        return {}
    return result


def _mask_credential_value(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mask all values in a credential dict for API responses."""
    if not value:
        return {}
    return {key: CREDENTIAL_MASK for key in value.keys()}


def decode_credential(
    row: asyncpg.Record,
    mask: bool = True,
) -> Credential:
    """
    Decode a single credential row.

    Args:
        row: Database row
        mask: If True, mask the value for API responses. If False, return real values.
    """
    real_value = _decrypt_and_parse_value(row["value"], row["is_encrypted"])

    return Credential(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        name=row["name"],
        credential_type=CredentialType(row["credential_type"]),
        value=_mask_credential_value(real_value) if mask else real_value,
        is_encrypted=row["is_encrypted"],
        description=row["description"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_credential_list(
    result: Optional[List[asyncpg.Record]],
    mask: bool = True,
) -> List[Credential]:
    """Decode multiple credential rows."""
    if not result:
        return []
    return [decode_credential(row, mask=mask) for row in result]


def decode_single_credential(
    result: Optional[List[asyncpg.Record]],
    mask: bool = True,
) -> Optional[Credential]:
    """Decode a single credential from query result."""
    if not result or len(result) == 0:
        return None
    return decode_credential(result[0], mask=mask)


def decode_credentials_as_dict(
    result: Optional[List[asyncpg.Record]],
) -> Dict[str, Any]:
    """
    Decode credentials into a flat dict for template_vars resolution.
    Keys are credential names, values are the first value from the credential dict.

    For api_key type: {"shopify_api_key": "sk-xxx"}
    For bearer_token: {"api_token": "eyJ..."}
    For basic_auth: {"api_username": "user", "api_password": "pass"}
    For custom: all key-value pairs are flattened

    If multiple credentials share the same key, reseller-specific overrides global
    (query must ORDER BY reseller_id NULLS FIRST).
    """
    if not result:
        return {}

    merged: Dict[str, Any] = {}

    for row in result:
        real_value = _decrypt_and_parse_value(row["value"], row["is_encrypted"])
        if not real_value:
            continue

        cred_type = row["credential_type"]
        cred_name = row["name"]

        if cred_type == "api_key":
            # Store as name -> key value
            merged[cred_name] = real_value.get("key", "")
        elif cred_type == "bearer_token":
            merged[cred_name] = real_value.get("token", "")
        elif cred_type == "basic_auth":
            # Store as name_username and name_password
            merged[f"{cred_name}_username"] = real_value.get("username", "")
            merged[f"{cred_name}_password"] = real_value.get("password", "")
        elif cred_type == "custom":
            # Flatten all key-value pairs
            merged.update(real_value)

    return merged
