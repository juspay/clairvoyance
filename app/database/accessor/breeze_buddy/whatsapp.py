"""WhatsApp helpers backed by the generic connector database layer."""

from datetime import datetime, timezone
from typing import Optional

from app.core.logger import logger
from app.database.accessor.breeze_buddy.connectors import (
    disconnect_connector,
    get_active_connector,
    get_active_connector_credential,
    get_connector,
    increment_connector_metric,
    sync_connector_connection,
)
from app.schemas.breeze_buddy.connectors import (
    Connector,
    ConnectorMetricIncrement,
    UpsertConnectorConnection,
)
from app.schemas.breeze_buddy.whatsapp import (
    SyncMerchantWhatsAppConnection,
    WhatsAppCredentialSecret,
)
from app.services.encryption import (
    encrypt_credential,
    is_credential_encryption_configured,
)
from app.services.whatsapp_sync_encryption import decrypt_whatsapp_access_token_envelope

_WHATSAPP_CONNECTOR = "whatsapp"


def _encrypt_whatsapp_credential_secret(access_token: str) -> Optional[str]:
    """Encrypt a Meta access token for connector credential storage."""
    if not is_credential_encryption_configured():
        logger.error("CREDENTIAL_ENCRYPTION_KEY is required for WhatsApp token sync")
        return None

    stored_value, is_encrypted = encrypt_credential({"access_token": access_token})
    if not is_encrypted:
        logger.error("WhatsApp token credential encryption was not applied")
        return None
    return stored_value


def _whatsapp_metadata(connection: SyncMerchantWhatsAppConnection) -> dict:
    metadata = dict(connection.metadata)
    metadata.update(
        {
            "waba_id": connection.waba_id,
            "phone_number_id": connection.phone_number_id,
        }
    )
    if connection.shop_id is not None:
        metadata["nautilus_shop_id"] = connection.shop_id
    if connection.template_name is not None:
        metadata["template_name"] = connection.template_name
    if connection.template_status is not None:
        metadata["template_status"] = connection.template_status
    return metadata


async def sync_merchant_whatsapp_connection(
    connection: SyncMerchantWhatsAppConnection,
) -> Optional[Connector]:
    """Decrypt and atomically persist a Nautilus WhatsApp connection."""
    try:
        access_token = decrypt_whatsapp_access_token_envelope(
            connection.encrypted_access_token
        )
        credential_value = _encrypt_whatsapp_credential_secret(access_token)
        if credential_value is None:
            return None

        return await sync_connector_connection(
            UpsertConnectorConnection(
                reseller_id=connection.reseller_id,
                merchant_id=connection.merchant_id,
                connector=_WHATSAPP_CONNECTOR,
                credential_value=credential_value,
                credential_is_encrypted=True,
                credential_description="Clairvoyance-encrypted Meta WhatsApp access token",
                metadata=_whatsapp_metadata(connection),
            )
        )
    except Exception as e:
        logger.error(
            "Error syncing WhatsApp connection for "
            f"reseller={connection.reseller_id} merchant={connection.merchant_id}: {e}",
            exc_info=True,
        )
        return None


async def get_merchant_whatsapp_connector(
    reseller_id: str, merchant_id: str
) -> Optional[Connector]:
    return await get_connector(reseller_id, merchant_id, _WHATSAPP_CONNECTOR)


async def get_active_merchant_whatsapp_connector(
    reseller_id: str, merchant_id: str
) -> Optional[Connector]:
    return await get_active_connector(reseller_id, merchant_id, _WHATSAPP_CONNECTOR)


async def disconnect_merchant_whatsapp_connector(
    reseller_id: str, merchant_id: str
) -> bool:
    return await disconnect_connector(reseller_id, merchant_id, _WHATSAPP_CONNECTOR)


async def get_whatsapp_credential_secret(
    *, reseller_id: str, merchant_id: str, credential_id: str
) -> Optional[WhatsAppCredentialSecret]:
    credential = await get_active_connector_credential(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        connector=_WHATSAPP_CONNECTOR,
        credential_id=credential_id,
    )
    if not credential or not credential.value:
        return None
    try:
        return WhatsAppCredentialSecret(**credential.value)
    except Exception:
        return None


async def increment_merchant_whatsapp_message_counts(
    reseller_id: str,
    merchant_id: str,
    *,
    sent_increment: int = 0,
    failed_increment: int = 0,
) -> bool:
    """Record WhatsApp message outcomes as generic daily connector metrics."""
    if sent_increment < 0 or failed_increment < 0:
        return False
    if sent_increment == 0 and failed_increment == 0:
        return True

    connector = await get_active_merchant_whatsapp_connector(reseller_id, merchant_id)
    if not connector:
        return False

    metric_date = datetime.now(timezone.utc).date()
    succeeded = True
    if sent_increment:
        sent_metric = await increment_connector_metric(
            ConnectorMetricIncrement(
                connector_id=connector.id,
                reseller_id=reseller_id,
                merchant_id=merchant_id,
                metric_name="messages_sent",
                increment=sent_increment,
                metric_date=metric_date,
            )
        )
        succeeded = sent_metric is not None
    if failed_increment:
        failed_metric = await increment_connector_metric(
            ConnectorMetricIncrement(
                connector_id=connector.id,
                reseller_id=reseller_id,
                merchant_id=merchant_id,
                metric_name="messages_failed",
                increment=failed_increment,
                metric_date=metric_date,
            )
        )
        succeeded = succeeded and failed_metric is not None
    return succeeded
