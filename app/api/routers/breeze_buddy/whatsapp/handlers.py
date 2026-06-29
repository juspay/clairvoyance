"""Handlers for WhatsApp Embedded Signup endpoints."""

from typing import Optional

from fastapi import HTTPException, status

from app.core.config.static import (
    WHATSAPP_PAYMENT_LINK_TEMPLATE_CATEGORY,
    WHATSAPP_PAYMENT_LINK_TEMPLATE_LANGUAGE,
    WHATSAPP_PAYMENT_LINK_TEMPLATE_NAME,
)
from app.core.logger import logger
from app.core.security.scope import resolve_merchant_ids
from app.database.accessor.breeze_buddy import merchants as merchant_accessors
from app.database.accessor.breeze_buddy.whatsapp import (
    disconnect_whatsapp_connection,
    get_whatsapp_business_token_by_merchant_id,
    get_whatsapp_connection_by_merchant_id,
    increment_whatsapp_message_counter,
    update_whatsapp_connection_setup_status,
    upsert_whatsapp_connection,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.whatsapp import (
    MetaEmbeddedSignupCompleteRequest,
    WhatsAppConnectionResponse,
    WhatsAppConnectionStatus,
    WhatsAppEmbeddedSignupConfigResponse,
    WhatsAppRegisterPhoneRequest,
    WhatsAppSendPaymentLinkRequest,
    WhatsAppSendPaymentLinkResponse,
)
from app.services.meta.whatsapp import (
    MetaWhatsAppAPIError,
    MetaWhatsAppClient,
    MetaWhatsAppConfigurationError,
)


async def _get_merchant_for_access(merchant_id: str):
    merchant = await merchant_accessors.get_merchant_by_merchant_identifier(merchant_id)
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merchant entity not found",
        )
    return merchant


async def _validate_merchant_access(
    current_user: UserInfo,
    merchant_id: str,
    operation: str,
) -> None:
    allowed = await resolve_merchant_ids(current_user)
    if allowed is None:
        return
    if merchant_id not in allowed:
        logger.warning(
            f"User {current_user.username} attempted to {operation} WhatsApp "
            f"connection for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to merchant {merchant_id}",
        )


def _not_connected_response(merchant_id: str) -> WhatsAppConnectionResponse:
    return WhatsAppConnectionResponse(
        merchant_id=merchant_id,
        status=WhatsAppConnectionStatus.NOT_CONNECTED,
        connected=False,
    )


def _configuration_http_error(exc: MetaWhatsAppConfigurationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(exc),
    )


def _meta_http_error(
    exc: MetaWhatsAppAPIError,
    fallback: str,
    status_code: int = status.HTTP_502_BAD_GATEWAY,
) -> HTTPException:
    detail = {"message": fallback}
    if exc.error_code:
        detail["meta_error_code"] = exc.error_code
    if exc.error_subcode:
        detail["meta_error_subcode"] = exc.error_subcode
    if str(exc):
        detail["meta_message"] = str(exc)
    return HTTPException(status_code=status_code, detail=detail)


async def get_whatsapp_embedded_signup_config_handler(
    merchant_id: str,
    current_user: UserInfo,
    meta_client: Optional[MetaWhatsAppClient] = None,
) -> WhatsAppEmbeddedSignupConfigResponse:
    """Return the frontend config needed to launch Meta Embedded Signup."""

    await _get_merchant_for_access(merchant_id)
    await _validate_merchant_access(current_user, merchant_id, "view")

    client = meta_client or MetaWhatsAppClient()
    try:
        client.ensure_configured()
    except MetaWhatsAppConfigurationError as e:
        raise _configuration_http_error(e)

    connection = await get_whatsapp_connection_by_merchant_id(merchant_id)
    response_connection = connection or None
    response_status = (
        connection.status if connection else WhatsAppConnectionStatus.NOT_CONNECTED
    )

    return WhatsAppEmbeddedSignupConfigResponse(
        merchant_id=merchant_id,
        app_id=client.app_id,
        config_id=client.embedded_signup_config_id,
        graph_api_version=client.graph_api_version,
        status=response_status,
        connected=response_status == WhatsAppConnectionStatus.CONNECTED,
        connection=response_connection,
    )


async def get_whatsapp_connection_handler(
    merchant_id: str,
    current_user: UserInfo,
) -> WhatsAppConnectionResponse:
    """Return merchant WhatsApp connection status."""

    await _get_merchant_for_access(merchant_id)
    await _validate_merchant_access(current_user, merchant_id, "view")

    connection = await get_whatsapp_connection_by_merchant_id(merchant_id)
    return connection or _not_connected_response(merchant_id)


async def complete_whatsapp_embedded_signup_handler(
    req: MetaEmbeddedSignupCompleteRequest,
    current_user: UserInfo,
    meta_client: Optional[MetaWhatsAppClient] = None,
) -> WhatsAppConnectionResponse:
    """Exchange Meta's code and store merchant WhatsApp credentials."""

    merchant = await _get_merchant_for_access(req.merchant_id)
    await _validate_merchant_access(current_user, req.merchant_id, "complete")

    if req.signup_event.type and req.signup_event.type != "WA_EMBEDDED_SIGNUP":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signup_event.type must be WA_EMBEDDED_SIGNUP",
        )

    session_info = req.signup_event.data
    if not session_info.phone_number_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signup_event.data.phone_number_id is required",
        )
    if not session_info.waba_id and not session_info.waba_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="signup_event.data.waba_id is required",
        )
    if not session_info.waba_id and session_info.waba_ids:
        session_info.waba_id = session_info.waba_ids[0]

    if req.register_phone_number and not req.phone_number_pin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="phone_number_pin is required when register_phone_number=true",
        )

    client = meta_client or MetaWhatsAppClient()
    try:
        token_result = await client.exchange_code_for_business_token(req.code)
    except MetaWhatsAppConfigurationError as e:
        raise _configuration_http_error(e)
    except MetaWhatsAppAPIError as e:
        raise _meta_http_error(e, "Meta token exchange failed")

    status_value = WhatsAppConnectionStatus.CONNECTED
    webhook_subscribed = False
    phone_registered = False
    last_error_code = None
    last_error_message = None
    template_result = None
    last_template_error = None

    if req.subscribe_webhooks:
        try:
            webhook_subscribed = await client.subscribe_app_to_waba(
                session_info.waba_id or "",
                token_result.access_token,
            )
            if not webhook_subscribed:
                status_value = WhatsAppConnectionStatus.ERROR
                last_error_message = "Meta webhook subscription returned success=false"
        except MetaWhatsAppAPIError as e:
            status_value = WhatsAppConnectionStatus.ERROR
            last_error_code = e.error_code
            last_error_message = f"Meta webhook subscription failed: {e}"

    if req.register_phone_number and req.phone_number_pin:
        try:
            phone_registered = await client.register_phone_number(
                session_info.phone_number_id,
                token_result.access_token,
                req.phone_number_pin,
            )
            if not phone_registered:
                status_value = WhatsAppConnectionStatus.ERROR
                last_error_message = "Meta phone registration returned success=false"
        except MetaWhatsAppAPIError as e:
            status_value = WhatsAppConnectionStatus.ERROR
            last_error_code = e.error_code
            last_error_message = f"Meta phone registration failed: {e}"

    try:
        template_result = await client.create_payment_link_utility_template(
            session_info.waba_id or "",
            token_result.access_token,
        )
    except MetaWhatsAppAPIError as e:
        status_value = WhatsAppConnectionStatus.ERROR
        last_error_code = e.error_code
        last_template_error = f"Meta template creation failed: {e}"
        if not last_error_message:
            last_error_message = last_template_error

    connection = await upsert_whatsapp_connection(
        reseller_id=merchant.reseller_id,
        merchant_id=req.merchant_id,
        session_info=session_info,
        token_result=token_result,
        app_id=client.app_id,
        config_id=client.embedded_signup_config_id,
        graph_api_version=client.graph_api_version,
        status=status_value,
        webhook_subscribed=webhook_subscribed,
        phone_registered=phone_registered,
        last_error_code=last_error_code,
        last_error_message=last_error_message,
        last_onboarding_event=req.signup_event.event,
        raw_signup_payload=req.signup_event.model_dump(mode="json"),
        template_result=template_result,
        template_name=WHATSAPP_PAYMENT_LINK_TEMPLATE_NAME,
        template_language=WHATSAPP_PAYMENT_LINK_TEMPLATE_LANGUAGE,
        template_category=WHATSAPP_PAYMENT_LINK_TEMPLATE_CATEGORY,
        last_template_error=last_template_error,
    )
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store WhatsApp connection",
        )
    return connection


async def register_whatsapp_phone_handler(
    merchant_id: str,
    req: WhatsAppRegisterPhoneRequest,
    current_user: UserInfo,
    meta_client: Optional[MetaWhatsAppClient] = None,
) -> WhatsAppConnectionResponse:
    """Register an existing merchant WhatsApp phone number with Cloud API."""

    await _get_merchant_for_access(merchant_id)
    await _validate_merchant_access(current_user, merchant_id, "register")

    connection = await get_whatsapp_connection_by_merchant_id(merchant_id)
    if not connection or not connection.phone_number_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp connection not found",
        )

    business_token = await get_whatsapp_business_token_by_merchant_id(merchant_id)
    if not business_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WhatsApp business token is unavailable",
        )

    client = meta_client or MetaWhatsAppClient()
    try:
        phone_registered = await client.register_phone_number(
            connection.phone_number_id,
            business_token,
            req.pin,
        )
    except MetaWhatsAppConfigurationError as e:
        raise _configuration_http_error(e)
    except MetaWhatsAppAPIError as e:
        updated = await update_whatsapp_connection_setup_status(
            merchant_id=merchant_id,
            status=WhatsAppConnectionStatus.ERROR,
            last_error_code=e.error_code,
            last_error_message=f"Meta phone registration failed: {e}",
        )
        if updated:
            return updated
        raise _meta_http_error(e, "Meta phone registration failed")

    status_value = (
        WhatsAppConnectionStatus.CONNECTED
        if phone_registered
        else WhatsAppConnectionStatus.ERROR
    )
    updated = await update_whatsapp_connection_setup_status(
        merchant_id=merchant_id,
        status=status_value,
        phone_registered=phone_registered,
        last_error_code=None if phone_registered else "registration_failed",
        last_error_message=(
            None
            if phone_registered
            else "Meta phone registration returned success=false"
        ),
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp connection not found",
        )
    return updated


async def send_whatsapp_payment_link_handler(
    merchant_id: str,
    req: WhatsAppSendPaymentLinkRequest,
    current_user: UserInfo,
    meta_client: Optional[MetaWhatsAppClient] = None,
) -> WhatsAppSendPaymentLinkResponse:
    """Send the merchant payment-link Utility template through WhatsApp."""

    await _get_merchant_for_access(merchant_id)
    await _validate_merchant_access(current_user, merchant_id, "send")

    connection = await get_whatsapp_connection_by_merchant_id(merchant_id)
    if not connection or not connection.phone_number_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp connection not found",
        )
    if connection.status != WhatsAppConnectionStatus.CONNECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="WhatsApp connection is not connected",
        )

    template_status = (connection.payment_link_template_status or "").upper()
    if template_status != "APPROVED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="WhatsApp payment link template is not approved",
        )

    business_token = await get_whatsapp_business_token_by_merchant_id(merchant_id)
    if not business_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WhatsApp business token is unavailable",
        )

    attempted = await increment_whatsapp_message_counter(
        merchant_id=merchant_id,
        counter="attempted",
    )
    if not attempted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp connection not found",
        )

    client = meta_client or MetaWhatsAppClient()
    try:
        result = await client.send_payment_link_template_message(
            phone_number_id=connection.phone_number_id,
            business_token=business_token,
            recipient_phone=req.recipient_phone,
            customer_name=req.customer_name,
            order_reference=req.order_reference,
            payment_link=req.payment_link,
            template_name=(
                connection.payment_link_template_name
                or WHATSAPP_PAYMENT_LINK_TEMPLATE_NAME
            ),
            language=(
                connection.payment_link_template_language
                or WHATSAPP_PAYMENT_LINK_TEMPLATE_LANGUAGE
            ),
        )
    except MetaWhatsAppConfigurationError as e:
        raise _configuration_http_error(e)
    except MetaWhatsAppAPIError as e:
        updated = await increment_whatsapp_message_counter(
            merchant_id=merchant_id,
            counter="failed",
            last_error_code=e.error_code,
            last_error_message=f"Meta WhatsApp payment link send failed: {e}",
        )
        if updated:
            attempted = updated
        raise _meta_http_error(e, "Meta WhatsApp payment link send failed")

    success = await increment_whatsapp_message_counter(
        merchant_id=merchant_id,
        counter="success",
    )
    return WhatsAppSendPaymentLinkResponse(
        message_id=result.message_id,
        connection=success or attempted,
    )


async def disconnect_whatsapp_connection_handler(
    merchant_id: str,
    current_user: UserInfo,
) -> WhatsAppConnectionResponse:
    """Mark the merchant WhatsApp connection disconnected locally."""

    await _get_merchant_for_access(merchant_id)
    await _validate_merchant_access(current_user, merchant_id, "disconnect")

    connection = await disconnect_whatsapp_connection(merchant_id)
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp connection not found",
        )
    return connection
