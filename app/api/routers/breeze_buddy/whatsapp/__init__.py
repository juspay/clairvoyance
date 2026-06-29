"""WhatsApp Embedded Signup endpoints."""

from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.whatsapp import (
    MetaEmbeddedSignupCompleteRequest,
    WhatsAppConnectionResponse,
    WhatsAppEmbeddedSignupConfigResponse,
    WhatsAppRegisterPhoneRequest,
    WhatsAppSendPaymentLinkRequest,
    WhatsAppSendPaymentLinkResponse,
)

from .handlers import (
    complete_whatsapp_embedded_signup_handler,
    disconnect_whatsapp_connection_handler,
    get_whatsapp_connection_handler,
    get_whatsapp_embedded_signup_config_handler,
    register_whatsapp_phone_handler,
    send_whatsapp_payment_link_handler,
)

router = APIRouter()


@router.get(
    "/whatsapp/embedded-signup/config",
    response_model=WhatsAppEmbeddedSignupConfigResponse,
)
async def get_whatsapp_embedded_signup_config(
    merchant_id: str = Query(..., description="Merchant ID to connect to WhatsApp"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Return Meta app/config IDs for Loom to launch WhatsApp Embedded Signup.
    """

    return await get_whatsapp_embedded_signup_config_handler(
        merchant_id=merchant_id,
        current_user=current_user,
    )


@router.post(
    "/whatsapp/embedded-signup/complete",
    response_model=WhatsAppConnectionResponse,
)
async def complete_whatsapp_embedded_signup(
    req: MetaEmbeddedSignupCompleteRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Exchange Meta's short-lived code and store the merchant WhatsApp credentials.
    """

    return await complete_whatsapp_embedded_signup_handler(req, current_user)


@router.get(
    "/whatsapp/connection/{merchant_id}",
    response_model=WhatsAppConnectionResponse,
)
async def get_whatsapp_connection(
    merchant_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Return merchant WhatsApp connection status."""

    return await get_whatsapp_connection_handler(merchant_id, current_user)


@router.post(
    "/whatsapp/connection/{merchant_id}/register-phone",
    response_model=WhatsAppConnectionResponse,
)
async def register_whatsapp_phone(
    merchant_id: str,
    req: WhatsAppRegisterPhoneRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Register a connected WhatsApp phone number for Cloud API messaging."""

    return await register_whatsapp_phone_handler(merchant_id, req, current_user)


@router.post(
    "/whatsapp/connection/{merchant_id}/send-payment-link",
    response_model=WhatsAppSendPaymentLinkResponse,
)
async def send_whatsapp_payment_link(
    merchant_id: str,
    req: WhatsAppSendPaymentLinkRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Send the approved payment-link WhatsApp template to a customer."""

    return await send_whatsapp_payment_link_handler(merchant_id, req, current_user)


@router.delete(
    "/whatsapp/connection/{merchant_id}",
    response_model=WhatsAppConnectionResponse,
)
async def disconnect_whatsapp_connection_endpoint(
    merchant_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Mark a merchant WhatsApp connection disconnected locally."""

    return await disconnect_whatsapp_connection_handler(merchant_id, current_user)
