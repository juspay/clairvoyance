"""Decoder functions for merchant WhatsApp credentials."""

from datetime import datetime
from typing import Any, Dict, Optional

import asyncpg

from app.core.logger import logger
from app.schemas.breeze_buddy.whatsapp import (
    MerchantWhatsAppCredentials,
    MerchantWhatsAppCredentialsWithSecrets,
)
from app.services.encryption import decrypt_credential


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse a datetime value from stored credential JSON."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _decrypt_whatsapp_value(row: asyncpg.Record) -> Optional[Dict[str, Any]]:
    """Decrypt the credential JSON payload for internal WhatsApp sends."""
    result = decrypt_credential(
        row["credential_value"],
        row["credential_is_encrypted"],
    )
    if result is None:
        logger.error("Failed to decrypt WhatsApp credential value")
        return None
    return result


def _base_whatsapp_payload(row: asyncpg.Record) -> Dict[str, Any]:
    """Build the non-sensitive WhatsApp credential payload."""
    return {
        "id": str(row["whatsapp_credentials_id"]),
        "credential_id": str(row["credential_id"]),
        "reseller_id": row["reseller_id"],
        "merchant_id": row["merchant_id"],
        "business_id": row["business_id"],
        "waba_id": row["waba_id"],
        "phone_number_id": row["phone_number_id"],
        "display_phone_number": row["display_phone_number"],
        "verified_name": row["verified_name"],
        "is_connected": bool(row["is_active"] and row["phone_number_id"]),
        "messages_sent_count": int(row["messages_sent_count"] or 0),
        "connected_at": row["connected_at"],
        "last_message_sent_at": row["last_message_sent_at"],
        "created_at": row["whatsapp_created_at"],
        "updated_at": row["whatsapp_updated_at"],
    }


def decode_merchant_whatsapp_credentials(
    row: asyncpg.Record,
    include_secrets: bool = False,
) -> Optional[MerchantWhatsAppCredentials | MerchantWhatsAppCredentialsWithSecrets]:
    """Decode a merchant WhatsApp credential row."""
    payload = _base_whatsapp_payload(row)

    if not include_secrets:
        return MerchantWhatsAppCredentials(**payload)

    secret_value = _decrypt_whatsapp_value(row)
    if not secret_value or not secret_value.get("access_token"):
        logger.error(
            "WhatsApp credential secret is unavailable for merchant "
            f"{row['merchant_id']}"
        )
        return None

    payload.update(
        {
            "access_token": secret_value.get("access_token"),
            "token_type": secret_value.get("token_type"),
            "access_token_expires_at": _parse_datetime(
                secret_value.get("access_token_expires_at")
            ),
        }
    )
    return MerchantWhatsAppCredentialsWithSecrets(**payload)


def decode_single_merchant_whatsapp_credentials(
    result: Optional[list[asyncpg.Record]],
    include_secrets: bool = False,
) -> Optional[MerchantWhatsAppCredentials | MerchantWhatsAppCredentialsWithSecrets]:
    """Decode one merchant WhatsApp credential record from query result."""
    if not result:
        return None
    return decode_merchant_whatsapp_credentials(
        result[0],
        include_secrets=include_secrets,
    )
