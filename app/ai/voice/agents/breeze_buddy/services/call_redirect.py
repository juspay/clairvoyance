"""
Shared call redirect utility.

Redirects a live call to another phone number using the conference/transfer
mechanism supported by Exotel and Plivo.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import set_transfer_flag
from app.core.config.static import APP_BASE_URL
from app.core.logger import logger
from app.database.accessor import get_telephony_number_by_id


async def redirect_call(
    call_sid: str,
    redirect_number: str,
    telephony_number_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    telephony_service: VoiceCallProvider,
    provider: str,
    customer_phone_number: Optional[str] = None,
) -> bool:
    """
    Redirect a live call to another phone number via conference transfer.

    Returns True on success, False on failure.
    """
    try:
        outbound_record = await get_telephony_number_by_id(telephony_number_id)
        if not outbound_record:
            logger.error(f"[REDIRECT] Telephony number {telephony_number_id} not found")
            return False

        if telephony_service.conference_service is None:
            logger.error(f"[REDIRECT] conference_service is None for call {call_sid}")
            return False

        provider_name = provider.lower()
        f"{APP_BASE_URL}/agent/voice/breeze-buddy/{provider_name}/callback/transfer/conference-end"

        result = await telephony_service.conference_service.handle_transfer(
            conference_name=f"redirect-{call_sid}",
            agent_phone_number=redirect_number,
            customer_call_sid=call_sid,
            telephony_number=outbound_record.number,
        )

        if result.get("success"):
            # Set transfer flag only after successful transfer (Fix #5 + #12)
            await set_transfer_flag(
                call_sid=call_sid,
                reseller_id=reseller_id,
                merchant_id=merchant_id or "",
                transfer_number=redirect_number,
                customer_phone_number=customer_phone_number,
            )
            logger.info(f"[REDIRECT] Call {call_sid} redirected to {redirect_number}")
            return True

        logger.warning(f"[REDIRECT] Failed for call {call_sid}: {result.get('reason')}")
        return False

    except Exception as e:
        logger.error(f"[REDIRECT] Exception for call {call_sid}: {e}", exc_info=True)
        return False
