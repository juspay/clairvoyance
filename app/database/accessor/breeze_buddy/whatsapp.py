"""Database accessors for merchant WhatsApp credentials."""

from typing import Any, Dict, Optional
from uuid import uuid4

from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.whatsapp import (
    decode_single_merchant_whatsapp_credentials,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.whatsapp import (
    deactivate_merchant_whatsapp_credentials_query,
    get_any_merchant_whatsapp_credentials_query,
    get_merchant_whatsapp_credentials_query,
    increment_merchant_whatsapp_messages_sent_query,
    insert_whatsapp_secret_credential_query,
    update_whatsapp_secret_credential_query,
    upsert_merchant_whatsapp_metadata_query,
)
from app.schemas.breeze_buddy.whatsapp import (
    MerchantWhatsAppCredentials,
    MerchantWhatsAppCredentialsWithSecrets,
    UpsertMerchantWhatsAppCredentials,
)
from app.services.encryption import encrypt_credential


def _build_secret_value(
    credentials: UpsertMerchantWhatsAppCredentials,
) -> Dict[str, Any]:
    """Build encrypted JSON payload for Meta WhatsApp credentials."""
    return {
        "access_token": credentials.access_token,
        "token_type": credentials.token_type,
        "access_token_expires_at": (
            credentials.access_token_expires_at.isoformat()
            if credentials.access_token_expires_at
            else None
        ),
        "business_id": credentials.business_id,
        "waba_id": credentials.waba_id,
        "phone_number_id": credentials.phone_number_id,
        "display_phone_number": credentials.display_phone_number,
        "verified_name": credentials.verified_name,
    }


async def upsert_merchant_whatsapp_credentials(
    credentials: UpsertMerchantWhatsAppCredentials,
) -> Optional[MerchantWhatsAppCredentials]:
    """Create or replace merchant WhatsApp credentials."""
    logger.info(
        "Upserting WhatsApp credentials for merchant "
        f"{credentials.merchant_id} under reseller {credentials.reseller_id}"
    )

    try:
        stored_value, is_encrypted = encrypt_credential(
            _build_secret_value(credentials)
        )

        async for conn in get_db_connection():
            async with conn.transaction():
                existing_query, existing_values = (
                    get_any_merchant_whatsapp_credentials_query(
                        reseller_id=credentials.reseller_id,
                        merchant_id=credentials.merchant_id,
                    )
                )
                existing_rows = await conn.fetch(existing_query, *existing_values)

                if existing_rows:
                    credential_id = str(existing_rows[0]["credential_id"])
                    credential_query, credential_values = (
                        update_whatsapp_secret_credential_query(
                            credential_id=credential_id,
                            value=stored_value,
                            is_encrypted=is_encrypted,
                        )
                    )
                else:
                    credential_id = str(uuid4())
                    credential_query, credential_values = (
                        insert_whatsapp_secret_credential_query(
                            id=credential_id,
                            reseller_id=credentials.reseller_id,
                            merchant_id=credentials.merchant_id,
                            value=stored_value,
                            is_encrypted=is_encrypted,
                        )
                    )

                await conn.fetch(credential_query, *credential_values)

                metadata_query, metadata_values = (
                    upsert_merchant_whatsapp_metadata_query(
                        id=str(uuid4()),
                        reseller_id=credentials.reseller_id,
                        merchant_id=credentials.merchant_id,
                        credential_id=credential_id,
                        business_id=credentials.business_id,
                        waba_id=credentials.waba_id,
                        phone_number_id=credentials.phone_number_id,
                        display_phone_number=credentials.display_phone_number,
                        verified_name=credentials.verified_name,
                    )
                )
                await conn.fetch(metadata_query, *metadata_values)

                result_query, result_values = get_merchant_whatsapp_credentials_query(
                    reseller_id=credentials.reseller_id,
                    merchant_id=credentials.merchant_id,
                )
                result = await conn.fetch(result_query, *result_values)
                decoded = decode_single_merchant_whatsapp_credentials(result)
                if isinstance(decoded, MerchantWhatsAppCredentials):
                    return decoded
                return None

        return None
    except Exception as e:
        logger.error(
            f"Error upserting WhatsApp credentials for {credentials.merchant_id}: {e}",
            exc_info=True,
        )
        return None


async def get_merchant_whatsapp_credentials(
    reseller_id: str,
    merchant_id: str,
    include_secrets: bool = False,
) -> Optional[MerchantWhatsAppCredentials | MerchantWhatsAppCredentialsWithSecrets]:
    """Get active WhatsApp credentials for a merchant."""
    try:
        query_text, values = get_merchant_whatsapp_credentials_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
        )
        result = await run_parameterized_query(query_text, values)
        return decode_single_merchant_whatsapp_credentials(
            result,
            include_secrets=include_secrets,
        )
    except Exception as e:
        logger.error(
            f"Error getting WhatsApp credentials for merchant {merchant_id}: {e}",
            exc_info=True,
        )
        return None


async def is_merchant_whatsapp_connected(
    reseller_id: str,
    merchant_id: str,
) -> bool:
    """Return whether a merchant has active WhatsApp credentials."""
    credentials = await get_merchant_whatsapp_credentials(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
    )
    return bool(credentials and credentials.is_connected)


async def deactivate_merchant_whatsapp_credentials(
    reseller_id: str,
    merchant_id: str,
) -> bool:
    """Deactivate WhatsApp credentials for a merchant."""
    try:
        query_text, values = deactivate_merchant_whatsapp_credentials_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
        )
        result = await run_parameterized_query(query_text, values)
        return bool(result)
    except Exception as e:
        logger.error(
            f"Error deactivating WhatsApp credentials for merchant {merchant_id}: {e}",
            exc_info=True,
        )
        return False


async def increment_merchant_whatsapp_messages_sent(
    reseller_id: str,
    merchant_id: str,
    increment_by: int = 1,
) -> bool:
    """Increment merchant-level WhatsApp sent-message count."""
    try:
        if increment_by <= 0:
            logger.warning(
                "Invalid WhatsApp sent-count increment for merchant "
                f"{merchant_id}: {increment_by}"
            )
            return False

        query_text, values = increment_merchant_whatsapp_messages_sent_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            increment_by=increment_by,
        )
        result = await run_parameterized_query(query_text, values)
        return bool(result)
    except Exception as e:
        logger.error(
            f"Error incrementing WhatsApp sent count for merchant {merchant_id}: {e}",
            exc_info=True,
        )
        return False
