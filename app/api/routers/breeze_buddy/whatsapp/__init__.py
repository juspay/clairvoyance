"""WhatsApp connection management endpoints for Breeze Buddy."""

from fastapi import APIRouter, Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.whatsapp import (
    SyncMerchantWhatsAppConnection,
    WhatsAppConnectionDisconnectRequest,
    WhatsAppConnectionDisconnectResponse,
    WhatsAppConnectionSyncResponse,
)

from .handlers import (
    disconnect_whatsapp_connection_handler,
    sync_whatsapp_connection_handler,
)
from .rbac import require_whatsapp_connection_access

router = APIRouter()


@router.post(
    "/whatsapp/connection-sync",
    response_model=WhatsAppConnectionSyncResponse,
)
async def sync_whatsapp_connection_endpoint(
    req: SyncMerchantWhatsAppConnection,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Sync a merchant WhatsApp connection created by Nautilus.

    The request contains only a Nautilus-encrypted Meta access token. Clairvoyance
    stores it without returning or logging the token payload.
    """
    require_whatsapp_connection_access(
        current_user,
        reseller_id=req.reseller_id,
        merchant_id=req.merchant_id,
        operation="connection sync",
    )
    return await sync_whatsapp_connection_handler(req, current_user)


@router.post(
    "/whatsapp/connection-disconnect",
    response_model=WhatsAppConnectionDisconnectResponse,
)
async def disconnect_whatsapp_connection_endpoint(
    req: WhatsAppConnectionDisconnectRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Disconnect a merchant WhatsApp connection synced from Nautilus."""
    require_whatsapp_connection_access(
        current_user,
        reseller_id=req.reseller_id,
        merchant_id=req.merchant_id,
        operation="connection disconnect",
    )
    return await disconnect_whatsapp_connection_handler(req, current_user)
