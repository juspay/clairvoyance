"""Database query functions for merchant WhatsApp credentials."""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

MERCHANT_WHATSAPP_CREDENTIALS_TABLE = "merchant_whatsapp_credentials"


def upsert_whatsapp_connection_query(
    id: str,
    reseller_id: Optional[str],
    merchant_id: str,
    business_token_credential_id: str,
    meta_business_id: Optional[str],
    waba_id: str,
    phone_number_id: str,
    display_phone_number: Optional[str],
    verified_name: Optional[str],
    token_type: Optional[str],
    token_expires_at: Optional[datetime],
    scope: Optional[str],
    app_id: Optional[str],
    config_id: Optional[str],
    graph_api_version: str,
    status: str,
    webhook_subscribed: bool,
    phone_registered: bool,
    last_error_code: Optional[str],
    last_error_message: Optional[str],
    last_onboarding_event: Optional[str],
    raw_signup_payload_json: str,
    payment_link_template_id: Optional[str],
    payment_link_template_name: Optional[str],
    payment_link_template_language: Optional[str],
    payment_link_template_category: Optional[str],
    payment_link_template_status: Optional[str],
    payment_link_template_created_at: Optional[datetime],
    last_template_error: Optional[str],
) -> Tuple[str, List[Any]]:
    """Generate upsert query for a merchant WhatsApp connection."""

    now = datetime.now(timezone.utc)
    text = f"""
        INSERT INTO {MERCHANT_WHATSAPP_CREDENTIALS_TABLE} (
            id, reseller_id, merchant_id, business_token_credential_id,
            meta_business_id, waba_id, phone_number_id, display_phone_number,
            verified_name, token_type, token_expires_at, scope, app_id,
            config_id, graph_api_version, status, webhook_subscribed,
            phone_registered, last_error_code, last_error_message,
            last_onboarding_event, raw_signup_payload, payment_link_template_id,
            payment_link_template_name, payment_link_template_language,
            payment_link_template_category, payment_link_template_status,
            payment_link_template_created_at, last_template_error,
            connected_at, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7, $8,
            $9, $10, $11, $12, $13,
            $14, $15, $16, $17,
            $18, $19, $20,
            $21, $22::jsonb, $23,
            $24, $25,
            $26, $27,
            $28, $29,
            $30, $31, $32
        )
        ON CONFLICT (merchant_id) DO UPDATE SET
            reseller_id = EXCLUDED.reseller_id,
            business_token_credential_id = EXCLUDED.business_token_credential_id,
            meta_business_id = EXCLUDED.meta_business_id,
            waba_id = EXCLUDED.waba_id,
            phone_number_id = EXCLUDED.phone_number_id,
            display_phone_number = EXCLUDED.display_phone_number,
            verified_name = EXCLUDED.verified_name,
            token_type = EXCLUDED.token_type,
            token_expires_at = EXCLUDED.token_expires_at,
            scope = EXCLUDED.scope,
            app_id = EXCLUDED.app_id,
            config_id = EXCLUDED.config_id,
            graph_api_version = EXCLUDED.graph_api_version,
            status = EXCLUDED.status,
            webhook_subscribed = EXCLUDED.webhook_subscribed,
            phone_registered = EXCLUDED.phone_registered,
            last_error_code = EXCLUDED.last_error_code,
            last_error_message = EXCLUDED.last_error_message,
            last_onboarding_event = EXCLUDED.last_onboarding_event,
            raw_signup_payload = EXCLUDED.raw_signup_payload,
            payment_link_template_id = EXCLUDED.payment_link_template_id,
            payment_link_template_name = EXCLUDED.payment_link_template_name,
            payment_link_template_language = EXCLUDED.payment_link_template_language,
            payment_link_template_category = EXCLUDED.payment_link_template_category,
            payment_link_template_status = EXCLUDED.payment_link_template_status,
            payment_link_template_created_at = EXCLUDED.payment_link_template_created_at,
            last_template_error = EXCLUDED.last_template_error,
            connected_at = CASE
                WHEN EXCLUDED.status = 'CONNECTED' THEN EXCLUDED.connected_at
                ELSE {MERCHANT_WHATSAPP_CREDENTIALS_TABLE}.connected_at
            END,
            updated_at = EXCLUDED.updated_at
        RETURNING *;
    """
    values = [
        id,
        reseller_id,
        merchant_id,
        business_token_credential_id,
        meta_business_id,
        waba_id,
        phone_number_id,
        display_phone_number,
        verified_name,
        token_type,
        token_expires_at,
        scope,
        app_id,
        config_id,
        graph_api_version,
        status,
        webhook_subscribed,
        phone_registered,
        last_error_code,
        last_error_message,
        last_onboarding_event,
        raw_signup_payload_json,
        payment_link_template_id,
        payment_link_template_name,
        payment_link_template_language,
        payment_link_template_category,
        payment_link_template_status,
        payment_link_template_created_at,
        last_template_error,
        now if status == "CONNECTED" else None,
        now,
        now,
    ]
    return text, values


def get_whatsapp_connection_by_merchant_id_query(
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """Generate query to fetch WhatsApp connection by merchant ID."""

    text = f"""
        SELECT *
        FROM {MERCHANT_WHATSAPP_CREDENTIALS_TABLE}
        WHERE merchant_id = $1;
    """
    return text, [merchant_id]


def list_whatsapp_connections_by_reseller_id_query(
    reseller_id: str,
) -> Tuple[str, List[Any]]:
    """Generate query to list WhatsApp connections by reseller ID."""

    text = f"""
        SELECT *
        FROM {MERCHANT_WHATSAPP_CREDENTIALS_TABLE}
        WHERE reseller_id = $1
        ORDER BY updated_at DESC;
    """
    return text, [reseller_id]


def update_whatsapp_connection_setup_status_query(
    merchant_id: str,
    status: str,
    webhook_subscribed: Optional[bool] = None,
    phone_registered: Optional[bool] = None,
    last_error_code: Optional[str] = None,
    last_error_message: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to update setup flags after onboarding actions."""

    updates = ["status = $2", "updated_at = $3"]
    values: List[Any] = [merchant_id, status, datetime.now(timezone.utc)]
    param_idx = 4

    if webhook_subscribed is not None:
        updates.append(f"webhook_subscribed = ${param_idx}")
        values.append(webhook_subscribed)
        param_idx += 1

    if phone_registered is not None:
        updates.append(f"phone_registered = ${param_idx}")
        values.append(phone_registered)
        param_idx += 1

    updates.append(f"last_error_code = ${param_idx}")
    values.append(last_error_code)
    param_idx += 1

    updates.append(f"last_error_message = ${param_idx}")
    values.append(last_error_message)

    text = f"""
        UPDATE {MERCHANT_WHATSAPP_CREDENTIALS_TABLE}
        SET {", ".join(updates)}
        WHERE merchant_id = $1
        RETURNING *;
    """
    return text, values


def increment_whatsapp_message_counter_query(
    merchant_id: str,
    counter: str,
    last_error_code: Optional[str] = None,
    last_error_message: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to increment a simple WhatsApp message counter."""

    counter_columns = {
        "attempted": ("messages_attempted_count", "last_message_attempted_at"),
        "success": ("messages_success_count", "last_message_success_at"),
        "failed": ("messages_failed_count", "last_message_failed_at"),
    }
    if counter not in counter_columns:
        raise ValueError(f"Unsupported WhatsApp message counter: {counter}")

    count_column, timestamp_column = counter_columns[counter]
    now = datetime.now(timezone.utc)
    text = f"""
        UPDATE {MERCHANT_WHATSAPP_CREDENTIALS_TABLE}
        SET {count_column} = {count_column} + 1,
            {timestamp_column} = $2,
            last_error_code = $3,
            last_error_message = $4,
            updated_at = $2
        WHERE merchant_id = $1
        RETURNING *;
    """
    return text, [merchant_id, now, last_error_code, last_error_message]


def disconnect_whatsapp_connection_query(
    merchant_id: str,
) -> Tuple[str, List[Any]]:
    """Generate query to mark a merchant WhatsApp connection disconnected."""

    text = f"""
        UPDATE {MERCHANT_WHATSAPP_CREDENTIALS_TABLE}
        SET status = 'DISCONNECTED',
            updated_at = $2
        WHERE merchant_id = $1
        RETURNING *;
    """
    return text, [merchant_id, datetime.now(timezone.utc)]
