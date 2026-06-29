"""Database accessor functions for merchant WhatsApp credentials."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_credential_by_id,
    upsert_merchant_credential_by_name,
)
from app.database.decoder.breeze_buddy.whatsapp import (
    decode_single_whatsapp_connection,
    decode_whatsapp_connection_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.whatsapp import (
    disconnect_whatsapp_connection_query,
    get_whatsapp_connection_by_merchant_id_query,
    increment_whatsapp_message_counter_query,
    list_whatsapp_connections_by_reseller_id_query,
    update_whatsapp_connection_setup_status_query,
    upsert_whatsapp_connection_query,
)
from app.schemas import CredentialType
from app.schemas.breeze_buddy.whatsapp import (
    MetaEmbeddedSignupSessionInfo,
    MetaTemplateCreateResult,
    MetaTokenExchangeResult,
    WhatsAppConnectionResponse,
    WhatsAppConnectionStatus,
)

WHATSAPP_BUSINESS_TOKEN_CREDENTIAL_NAME = "whatsapp_business_token"


def _token_expires_at(expires_in: Optional[int]) -> Optional[datetime]:
    """Convert a Meta expires_in value to an absolute UTC timestamp."""

    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=expires_in)


async def upsert_whatsapp_connection(
    reseller_id: Optional[str],
    merchant_id: str,
    session_info: MetaEmbeddedSignupSessionInfo,
    token_result: MetaTokenExchangeResult,
    app_id: Optional[str],
    config_id: Optional[str],
    graph_api_version: str,
    status: WhatsAppConnectionStatus,
    webhook_subscribed: bool,
    phone_registered: bool,
    last_error_code: Optional[str],
    last_error_message: Optional[str],
    last_onboarding_event: Optional[str],
    raw_signup_payload: Dict[str, Any],
    template_result: Optional[MetaTemplateCreateResult] = None,
    template_name: Optional[str] = None,
    template_language: Optional[str] = None,
    template_category: Optional[str] = None,
    last_template_error: Optional[str] = None,
) -> Optional[WhatsAppConnectionResponse]:
    """Create or update a merchant WhatsApp connection."""

    token_credential = await upsert_merchant_credential_by_name(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        name=WHATSAPP_BUSINESS_TOKEN_CREDENTIAL_NAME,
        credential_type=CredentialType.BEARER_TOKEN,
        value={"token": token_result.access_token},
        description="Meta WhatsApp business token from Embedded Signup",
        mask=False,
    )
    if not token_credential:
        raise RuntimeError("Failed to store WhatsApp business token credential")

    template_created_at = (
        datetime.now(timezone.utc)
        if template_result and (template_result.id or template_result.status)
        else None
    )
    query, values = upsert_whatsapp_connection_query(
        id=str(uuid4()),
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        business_token_credential_id=token_credential.id,
        meta_business_id=session_info.business_id,
        waba_id=session_info.waba_id or (session_info.waba_ids or [""])[0],
        phone_number_id=session_info.phone_number_id or "",
        display_phone_number=None,
        verified_name=None,
        token_type=token_result.token_type,
        token_expires_at=_token_expires_at(token_result.expires_in),
        scope=token_result.scope,
        app_id=app_id,
        config_id=config_id,
        graph_api_version=graph_api_version,
        status=status.value,
        webhook_subscribed=webhook_subscribed,
        phone_registered=phone_registered,
        last_error_code=last_error_code,
        last_error_message=last_error_message,
        last_onboarding_event=last_onboarding_event,
        raw_signup_payload_json=json.dumps(raw_signup_payload or {}),
        payment_link_template_id=template_result.id if template_result else None,
        payment_link_template_name=template_name,
        payment_link_template_language=template_language,
        payment_link_template_category=(
            template_result.category if template_result else template_category
        ),
        payment_link_template_status=(
            template_result.status if template_result else None
        ),
        payment_link_template_created_at=template_created_at,
        last_template_error=last_template_error,
    )

    try:
        result = await run_parameterized_query(query, values)
        return decode_single_whatsapp_connection(result)
    except Exception as e:
        logger.error(
            f"Error upserting WhatsApp connection for merchant {merchant_id}: {e}",
            exc_info=True,
        )
        raise


async def get_whatsapp_connection_by_merchant_id(
    merchant_id: str,
) -> Optional[WhatsAppConnectionResponse]:
    """Get a merchant WhatsApp connection by merchant ID."""

    query, values = get_whatsapp_connection_by_merchant_id_query(merchant_id)
    try:
        result = await run_parameterized_query(query, values)
        return decode_single_whatsapp_connection(result)
    except Exception as e:
        logger.error(
            f"Error fetching WhatsApp connection for merchant {merchant_id}: {e}"
        )
        raise


async def get_whatsapp_business_token_by_merchant_id(
    merchant_id: str,
) -> Optional[str]:
    """Get a decrypted WhatsApp business token for internal sending APIs."""

    query, values = get_whatsapp_connection_by_merchant_id_query(merchant_id)
    try:
        result = await run_parameterized_query(query, values)
        if not result:
            return None
        connection = decode_single_whatsapp_connection(result)
        if not connection or not connection.business_token_credential_id:
            return None

        credential = await get_credential_by_id(
            connection.business_token_credential_id,
            mask=False,
        )
        if not credential or not credential.value:
            return None

        token = credential.value.get("token")
        return token if isinstance(token, str) else None
    except Exception as e:
        logger.error(
            f"Error fetching WhatsApp business token for merchant {merchant_id}: {e}"
        )
        raise


async def list_whatsapp_connections_by_reseller_id(
    reseller_id: str,
) -> List[WhatsAppConnectionResponse]:
    """List merchant WhatsApp connections for a reseller."""

    query, values = list_whatsapp_connections_by_reseller_id_query(reseller_id)
    try:
        result = await run_parameterized_query(query, values)
        return decode_whatsapp_connection_list(result)
    except Exception as e:
        logger.error(
            f"Error listing WhatsApp connections for reseller {reseller_id}: {e}"
        )
        raise


async def update_whatsapp_connection_setup_status(
    merchant_id: str,
    status: WhatsAppConnectionStatus,
    webhook_subscribed: Optional[bool] = None,
    phone_registered: Optional[bool] = None,
    last_error_code: Optional[str] = None,
    last_error_message: Optional[str] = None,
) -> Optional[WhatsAppConnectionResponse]:
    """Update setup flags after webhook subscription or phone registration."""

    query, values = update_whatsapp_connection_setup_status_query(
        merchant_id=merchant_id,
        status=status.value,
        webhook_subscribed=webhook_subscribed,
        phone_registered=phone_registered,
        last_error_code=last_error_code,
        last_error_message=last_error_message,
    )
    result = await run_parameterized_query(query, values)
    return decode_single_whatsapp_connection(result)


async def increment_whatsapp_message_counter(
    merchant_id: str,
    counter: str,
    last_error_code: Optional[str] = None,
    last_error_message: Optional[str] = None,
) -> Optional[WhatsAppConnectionResponse]:
    """Increment a merchant-level WhatsApp message counter."""

    query, values = increment_whatsapp_message_counter_query(
        merchant_id=merchant_id,
        counter=counter,
        last_error_code=last_error_code,
        last_error_message=last_error_message,
    )
    result = await run_parameterized_query(query, values)
    return decode_single_whatsapp_connection(result)


async def disconnect_whatsapp_connection(
    merchant_id: str,
) -> Optional[WhatsAppConnectionResponse]:
    """Mark a merchant WhatsApp connection disconnected locally."""

    query, values = disconnect_whatsapp_connection_query(merchant_id)
    result = await run_parameterized_query(query, values)
    return decode_single_whatsapp_connection(result)
