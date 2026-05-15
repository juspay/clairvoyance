"""
Transfer Handler

Handles transfer to human agent by creating a conference call.
On success, terminates the AI conversation. On failure, continues gracefully.
"""

from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import set_transfer_flag
from app.core.config.static import APP_BASE_URL
from app.core.logger import logger
from app.database.accessor import get_outbound_number_by_id
from app.schemas import CallProvider


async def connect_to_live_agent(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initiate transfer to human agent via conference call.

    On success: Terminates AI conversation by calling end_conversation
    On failure: Returns gracefully, allowing AI conversation to continue

    Args:
        context: Handler context with bot state access
        args: LLM function arguments (not used for agent selection)
        transition_to: Not used

    Returns:
        Dict with status, reason, message, conference_id, agent_call_id
    """
    logger.info(f"Transfer called for {context.call_sid}")

    # Fetch outbound number from database
    if not context.lead or not context.lead.outbound_number_id:
        logger.error(
            f"Transfer failed for call {context.call_sid}: no outbound_number_id in lead"
        )
        return {
            "status": "failed",
            "reason": "missing_outbound_number_id",
            "message": "Outbound number not configured for this call",
        }

    outbound_number_record = await get_outbound_number_by_id(
        context.lead.outbound_number_id
    )
    if not outbound_number_record:
        logger.error(
            f"Transfer failed for call {context.call_sid}: outbound number not found"
        )
        return {
            "status": "failed",
            "reason": "outbound_number_not_found",
            "message": "Outbound number configuration not found",
        }

    outbound_number = outbound_number_record.number

    # Get transfer number from template configuration
    configurations = getattr(context.bot, "configurations", None)
    transfer_number = getattr(configurations, "transfer_number", None)

    if not transfer_number:
        logger.warning(
            f"No transfer number configured in template. Call {context.call_sid} will continue with AI."
        )
        return {
            "status": "failed",
            "reason": "transfer_number_not_configured",
            "message": "Transfer number is not configured for this assistant. Continuing with AI.",
        }

    agent_phone_number = transfer_number
    conference_name = f"transfer-{context.call_sid}"

    logger.info(
        f"Attempting transfer to {agent_phone_number}, conference: {conference_name}"
    )

    if not hasattr(context, "telephony_service") or context.telephony_service is None:
        logger.error(
            f"Transfer failed for call {context.call_sid}: no telephony_service available"
        )
        return {
            "status": "failed",
            "reason": "telephony_service_unavailable",
            "message": "Telephony service not configured",
        }

    if (
        not hasattr(context.telephony_service, "conference_service")
        or context.telephony_service.conference_service is None
    ):
        logger.error(
            f"Transfer failed for call {context.call_sid}: conference_service not available"
        )
        return {
            "status": "failed",
            "reason": "conference_service_unavailable",
            "message": "Conference service not configured",
        }

    try:
        customer_phone_number = None
        if context.lead and context.lead.payload:
            customer_phone_number = context.lead.payload.get("customer_mobile_number")
            if customer_phone_number:
                logger.info(f"Using customer phone number: {customer_phone_number}")
            else:
                logger.warning(
                    f"Customer phone number not found in payload for call {context.call_sid}"
                )

        # Set transfer flag in Redis (includes customer phone for Plivo dial-back)
        await set_transfer_flag(
            call_sid=context.call_sid,
            reseller_id=context.lead.reseller_id,
            merchant_id=context.lead.merchant_id,
            transfer_number=agent_phone_number,
            customer_phone_number=customer_phone_number,
        )
        logger.info(f"Transfer flag set in Redis for call {context.call_sid}")

        # Build status callback URL for conference events
        provider_name = context.provider.lower() if context.provider else None
        status_callback_url = f"{APP_BASE_URL}/agent/voice/breeze-buddy/{provider_name}/callback/transfer/conference-end"

        logger.info(f"Using conference status callback URL: {status_callback_url}")

        conference_result = (
            await context.telephony_service.conference_service.handle_transfer(
                conference_name=conference_name,
                agent_phone_number=agent_phone_number,
                customer_call_sid=context.call_sid,
                outbound_number=outbound_number,
                callback=None,
                status_callback_url=status_callback_url,
                customer_phone_number=customer_phone_number,
            )
        )

        if conference_result.get("success"):
            logger.info(
                f"Transfer successful: conference={conference_result.get('conference_id')}, "
                f"agent_call={conference_result.get('agent_call_id')}"
            )

            agent_call_id = conference_result.get("agent_call_id")
            conference_result.get("conference_id")

            transfer_meta = {
                "status": "success",
                "conference_id": conference_result.get("conference_id"),
                "agent_phone_number": agent_phone_number,
                "agent_call_id": agent_call_id,
            }

            if context.lead and hasattr(context.lead, "metaData"):
                if context.lead.metaData is None:
                    context.lead.metaData = {}
                context.lead.metaData["transfer"] = transfer_meta

            # For Plivo: suppress the serializer's auto hang-up before ending
            # the conversation. When end_conversation pushes EndFrame through
            # the pipeline the Plivo serializer would normally call _hang_up_call(),
            # which drops the caller from the conference. Setting _hangup_attempted=True
            # tells the serializer that a hang-up has already been handled so it
            # skips the API call.
            if context.provider == CallProvider.PLIVO:
                try:
                    plivo_serializer = context.bot.transport.output()._params.serializer
                    if plivo_serializer is not None:
                        plivo_serializer._hangup_attempted = True
                        logger.info(
                            f"[transfer_to_agent] Suppressed Plivo auto-hangup for call {context.call_sid}"
                        )
                except Exception as suppress_err:
                    logger.warning(
                        f"[transfer_to_agent] Could not suppress Plivo hangup: {suppress_err}"
                    )

            # End the AI conversation
            await end_conversation(context, None)

            if context.provider == CallProvider.PLIVO:
                if (
                    hasattr(context, "bot")
                    and hasattr(context.bot, "ws")
                    and context.bot.ws
                ):

                    logger.info(
                        f"Explicitly closing websocket for Plivo transfer on call {context.call_sid}"
                    )

                    await close_websocket_safely(
                        context.bot.ws, 1000, "Transfer complete"
                    )

            return {
                "status": "success",
                "conference_id": conference_result.get("conference_id"),
                "agent_call_id": agent_call_id,
                "message": "Successfully transferred to human agent",
            }
        else:
            failure_reason = conference_result.get("reason", "unknown_error")
            logger.warning(f"Transfer failed: {failure_reason}. AI continues.")

            return {
                "status": "failed",
                "reason": failure_reason,
                "message": f"Transfer failed: {failure_reason}. Continuing with AI assistant.",
            }

    except Exception as e:
        logger.error(f"Transfer exception: {str(e)}", exc_info=True)

        return {
            "status": "failed",
            "reason": "exception",
            "message": "Transfer failed due to error. Continuing with AI assistant.",
            "error": str(e),
        }
