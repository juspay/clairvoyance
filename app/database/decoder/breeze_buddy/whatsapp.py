"""Decoder functions for merchant WhatsApp credential rows."""

from typing import List, Optional

import asyncpg

from app.schemas.breeze_buddy.whatsapp import (
    WhatsAppConnectionResponse,
    WhatsAppConnectionStatus,
)


def decode_whatsapp_connection(row: asyncpg.Record) -> WhatsAppConnectionResponse:
    """Decode a merchant WhatsApp credential row for API responses."""

    status = WhatsAppConnectionStatus(row["status"])
    return WhatsAppConnectionResponse(
        merchant_id=row["merchant_id"],
        reseller_id=row["reseller_id"],
        business_token_credential_id=(
            str(row["business_token_credential_id"])
            if row["business_token_credential_id"]
            else None
        ),
        status=status,
        connected=status == WhatsAppConnectionStatus.CONNECTED,
        waba_id=row["waba_id"],
        phone_number_id=row["phone_number_id"],
        meta_business_id=row["meta_business_id"],
        display_phone_number=row["display_phone_number"],
        verified_name=row["verified_name"],
        app_id=row["app_id"],
        config_id=row["config_id"],
        graph_api_version=row["graph_api_version"],
        webhook_subscribed=row["webhook_subscribed"],
        phone_registered=row["phone_registered"],
        token_type=row["token_type"],
        token_expires_at=row["token_expires_at"],
        scope=row["scope"],
        payment_link_template_id=row["payment_link_template_id"],
        payment_link_template_name=row["payment_link_template_name"],
        payment_link_template_language=row["payment_link_template_language"],
        payment_link_template_category=row["payment_link_template_category"],
        payment_link_template_status=row["payment_link_template_status"],
        payment_link_template_created_at=row["payment_link_template_created_at"],
        payment_link_template_approved_at=row["payment_link_template_approved_at"],
        last_template_error=row["last_template_error"],
        messages_attempted_count=row["messages_attempted_count"] or 0,
        messages_success_count=row["messages_success_count"] or 0,
        messages_failed_count=row["messages_failed_count"] or 0,
        last_message_attempted_at=row["last_message_attempted_at"],
        last_message_success_at=row["last_message_success_at"],
        last_message_failed_at=row["last_message_failed_at"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        last_onboarding_event=row["last_onboarding_event"],
        connected_at=row["connected_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_whatsapp_connection_list(
    rows: Optional[List[asyncpg.Record]],
) -> List[WhatsAppConnectionResponse]:
    """Decode multiple merchant WhatsApp credential rows."""

    if not rows:
        return []
    return [decode_whatsapp_connection(row) for row in rows]


def decode_single_whatsapp_connection(
    rows: Optional[List[asyncpg.Record]],
) -> Optional[WhatsAppConnectionResponse]:
    """Decode a single merchant WhatsApp credential row."""

    if not rows:
        return None
    return decode_whatsapp_connection(rows[0])
