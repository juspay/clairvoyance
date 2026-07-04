"""Business logic handlers for WhatsApp connection sync endpoints."""

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.whatsapp import (
    disconnect_merchant_whatsapp_connector,
    sync_merchant_whatsapp_connection,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.whatsapp import (
    SyncMerchantWhatsAppConnection,
    WhatsAppConnectionDisconnectRequest,
    WhatsAppConnectionDisconnectResponse,
    WhatsAppConnectionSyncResponse,
)


async def sync_whatsapp_connection_handler(
    req: SyncMerchantWhatsAppConnection,
    current_user: UserInfo,
) -> WhatsAppConnectionSyncResponse:
    """Sync a Nautilus WhatsApp connection into Clairvoyance."""
    logger.info(
        f"User {current_user.username} syncing WhatsApp connection for "
        f"reseller={req.reseller_id} merchant={req.merchant_id}"
    )

    try:
        connector = await sync_merchant_whatsapp_connection(req)
        if not connector:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to sync WhatsApp connection",
            )

        return WhatsAppConnectionSyncResponse(
            status=connector.status,
            connector=connector,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Error syncing WhatsApp connection for "
            f"reseller={req.reseller_id} merchant={req.merchant_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to sync WhatsApp connection",
        )


async def disconnect_whatsapp_connection_handler(
    req: WhatsAppConnectionDisconnectRequest,
    current_user: UserInfo,
) -> WhatsAppConnectionDisconnectResponse:
    """Disconnect a merchant WhatsApp connection in Clairvoyance."""
    logger.info(
        f"User {current_user.username} disconnecting WhatsApp connection for "
        f"reseller={req.reseller_id} merchant={req.merchant_id}"
    )

    try:
        disconnected = await disconnect_merchant_whatsapp_connector(
            reseller_id=req.reseller_id,
            merchant_id=req.merchant_id,
        )
        if not disconnected:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="WhatsApp connection not found",
            )

        return WhatsAppConnectionDisconnectResponse(
            reseller_id=req.reseller_id,
            merchant_id=req.merchant_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Error disconnecting WhatsApp connection for "
            f"reseller={req.reseller_id} merchant={req.merchant_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disconnect WhatsApp connection",
        )
