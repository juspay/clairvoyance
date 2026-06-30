"""Database query helpers for merchant WhatsApp credentials."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.database.queries.breeze_buddy.credentials import CREDENTIALS_TABLE

MERCHANT_WHATSAPP_CREDENTIALS_TABLE = "merchant_whatsapp_credentials"
WHATSAPP_CREDENTIAL_NAME_PREFIX = "whatsapp_business_api:"
WHATSAPP_CREDENTIAL_TYPE = "custom"


def build_whatsapp_credential_name(merchant_id: str) -> str:
    """Build the hidden credentials-table name for a merchant WhatsApp token."""
    return f"{WHATSAPP_CREDENTIAL_NAME_PREFIX}{merchant_id}"


def _select_joined_whatsapp_credentials() -> str:
    """Return the joined select list used by WhatsApp credential queries."""
    return f"""
        SELECT
            mwc."id" AS "whatsapp_credentials_id",
            mwc."reseller_id",
            mwc."merchant_id",
            mwc."credential_id",
            mwc."business_id",
            mwc."waba_id",
            mwc."phone_number_id",
            mwc."display_phone_number",
            mwc."verified_name",
            mwc."messages_sent_count",
            mwc."connected_at",
            mwc."last_message_sent_at",
            mwc."is_active",
            mwc."created_at" AS "whatsapp_created_at",
            mwc."updated_at" AS "whatsapp_updated_at",
            c."value" AS "credential_value",
            c."is_encrypted" AS "credential_is_encrypted"
        FROM "{MERCHANT_WHATSAPP_CREDENTIALS_TABLE}" mwc
        JOIN "{CREDENTIALS_TABLE}" c ON c."id" = mwc."credential_id"
    """


def insert_whatsapp_secret_credential_query(
    *,
    id: str,
    reseller_id: str,
    merchant_id: str,
    value: str,
    is_encrypted: bool,
) -> Tuple[str, List[Any]]:
    """Generate query to insert the encrypted WhatsApp token credential."""
    now = datetime.now()
    text = f"""
        INSERT INTO "{CREDENTIALS_TABLE}"
        ("id", "reseller_id", "name", "credential_type", "value",
         "is_encrypted", "description", "is_active", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8, $9)
        RETURNING *;
    """
    return (
        text,
        [
            id,
            reseller_id,
            build_whatsapp_credential_name(merchant_id),
            WHATSAPP_CREDENTIAL_TYPE,
            value,
            is_encrypted,
            "WhatsApp Business API access token",
            now,
            now,
        ],
    )


def update_whatsapp_secret_credential_query(
    *,
    credential_id: str,
    value: str,
    is_encrypted: bool,
) -> Tuple[str, List[Any]]:
    """Generate query to update the encrypted WhatsApp token credential."""
    text = f"""
        UPDATE "{CREDENTIALS_TABLE}"
        SET "value" = $2,
            "is_encrypted" = $3,
            "is_active" = TRUE,
            "updated_at" = $4
        WHERE "id" = $1
        RETURNING *;
    """
    return text, [credential_id, value, is_encrypted, datetime.now()]


def upsert_merchant_whatsapp_metadata_query(
    *,
    id: str,
    reseller_id: str,
    merchant_id: str,
    credential_id: str,
    business_id: Optional[str],
    waba_id: str,
    phone_number_id: str,
    display_phone_number: Optional[str],
    verified_name: Optional[str],
) -> Tuple[str, List[Any]]:
    """Generate query to upsert merchant WhatsApp metadata."""
    now = datetime.now()
    text = f"""
        INSERT INTO "{MERCHANT_WHATSAPP_CREDENTIALS_TABLE}"
        ("id", "reseller_id", "merchant_id", "credential_id", "business_id",
         "waba_id", "phone_number_id", "display_phone_number", "verified_name",
         "connected_at", "is_active", "created_at", "updated_at")
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, TRUE, $11, $12)
        ON CONFLICT ("reseller_id", "merchant_id")
        DO UPDATE SET
            "credential_id" = EXCLUDED."credential_id",
            "business_id" = EXCLUDED."business_id",
            "waba_id" = EXCLUDED."waba_id",
            "phone_number_id" = EXCLUDED."phone_number_id",
            "display_phone_number" = EXCLUDED."display_phone_number",
            "verified_name" = EXCLUDED."verified_name",
            "connected_at" = EXCLUDED."connected_at",
            "is_active" = TRUE,
            "updated_at" = EXCLUDED."updated_at"
        RETURNING *;
    """
    return (
        text,
        [
            id,
            reseller_id,
            merchant_id,
            credential_id,
            business_id,
            waba_id,
            phone_number_id,
            display_phone_number,
            verified_name,
            now,
            now,
            now,
        ],
    )


def get_any_merchant_whatsapp_credentials_query(
    reseller_id: str,
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """Generate query to get WhatsApp metadata regardless of active state."""
    text = f"""
        {_select_joined_whatsapp_credentials()}
        WHERE mwc."reseller_id" = $1
          AND mwc."merchant_id" = $2
        LIMIT 1;
    """
    return text, [reseller_id, merchant_id]


def get_merchant_whatsapp_credentials_query(
    reseller_id: str,
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """Generate query to get active WhatsApp credentials for a merchant."""
    text = f"""
        {_select_joined_whatsapp_credentials()}
        WHERE mwc."reseller_id" = $1
          AND mwc."merchant_id" = $2
          AND mwc."is_active" = TRUE
          AND c."is_active" = TRUE
        LIMIT 1;
    """
    return text, [reseller_id, merchant_id]


def deactivate_merchant_whatsapp_credentials_query(
    reseller_id: str,
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """Generate query to deactivate merchant WhatsApp credentials and token."""
    text = f"""
        WITH deactivated_whatsapp AS (
            UPDATE "{MERCHANT_WHATSAPP_CREDENTIALS_TABLE}"
            SET "is_active" = FALSE,
                "updated_at" = $3
            WHERE "reseller_id" = $1
              AND "merchant_id" = $2
            RETURNING "credential_id"
        ),
        deactivated_credential AS (
            UPDATE "{CREDENTIALS_TABLE}" c
            SET "is_active" = FALSE,
                "updated_at" = $3
            FROM deactivated_whatsapp dw
            WHERE c."id" = dw."credential_id"
            RETURNING c."id"
        )
        SELECT "credential_id" FROM deactivated_whatsapp;
    """
    return text, [reseller_id, merchant_id, datetime.now()]


def increment_merchant_whatsapp_messages_sent_query(
    reseller_id: str,
    merchant_id: str,
    increment_by: int = 1,
) -> Tuple[str, List[Any]]:
    """Generate query to increment merchant-level WhatsApp sent-message count."""
    text = f"""
        UPDATE "{MERCHANT_WHATSAPP_CREDENTIALS_TABLE}"
        SET "messages_sent_count" = COALESCE("messages_sent_count", 0) + $3,
            "last_message_sent_at" = $4,
            "updated_at" = $4
        WHERE "reseller_id" = $1
          AND "merchant_id" = $2
          AND $3 > 0
          AND "is_active" = TRUE
        RETURNING "id";
    """
    return text, [reseller_id, merchant_id, increment_by, datetime.now()]
