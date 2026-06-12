"""
Transfer Handler

Handles transfer to human agent using Plivo's MultiPartyCall (MPC).

Flow (dial-first MPC):
  1. Agent is dialled into a new MPC via add_participant(role='agent').
     The customer's <Stream> is untouched and Buddy stays connected.
  2. We subscribe to a Redis pub/sub channel and wait for the MPC
     participant-state-changes webhook.
  3. Agent answers → webhook moves customer into MPC, publishes "answered".
     We end the AI conversation and close the WebSocket.
  4. Agent no-answer → webhook publishes "unavailable".
     Buddy continues the conversation seamlessly.
"""

import asyncio
from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import mute_stt, unmute_stt
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.hold_transfer import (
    publish_hold_transfer_result,
    subscribe_and_wait,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
)
from app.ai.voice.agents.breeze_buddy.utils.warm_transfer import set_transfer_flag
from app.core.logger import logger
from app.database.accessor import get_outbound_number_by_id
from app.schemas import CallProvider


async def connect_to_live_agent(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initiate warm transfer to human agent via Plivo MPC.

    On success (agent answers): terminates AI conversation.
    On failure (agent no-answer): returns gracefully, allowing AI to continue.

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

        # Mute STT before initiating transfer to prevent audio capture
        # during the ringing period (up to 35 seconds).
        await mute_stt(context, None)
        logger.info(f"STT muted for call {context.call_sid} before transfer")

        # Set transfer flag in Redis
        await set_transfer_flag(
            call_sid=context.call_sid,
            reseller_id=context.lead.reseller_id,
            merchant_id=context.lead.merchant_id,
            transfer_number=agent_phone_number,
            customer_phone_number=customer_phone_number,
        )
        logger.info(f"Transfer flag set in Redis for call {context.call_sid}")

        # Background subscriber — listens for webhook outcome.
        # Uses a generous timeout ceiling; the real deadline is enforced
        # by _timeout_publish below so both success and timeout paths
        # deliver a message to the same Redis channel.
        outcome_channel = f"transfer_outcome:{context.call_sid}"
        logger.info(f"[Transfer] Subscribing to outcome channel: {outcome_channel}")
        outcome_task = asyncio.create_task(
            subscribe_and_wait(outcome_channel, timeout_seconds=40)
        )

        # Background timer — publishes "unavailable" after 35s if no
        # webhook has fired yet.  This guarantees the subscriber always
        # receives a message (either "answered" from Plivo or
        # "unavailable" from us), eliminating a separate timeout branch.
        async def _timeout_publish():
            await asyncio.sleep(35)
            await publish_hold_transfer_result(
                outcome_channel, {"status": "unavailable"}
            )
            logger.info(
                f"[Transfer] Published 'unavailable' for call {context.call_sid} "
                f"after 35s timeout"
            )

        timeout_task = asyncio.create_task(_timeout_publish())

        # Trigger MPC transfer (agent is dialled into MPC, customer stays on Stream)
        conference_result = (
            await context.telephony_service.conference_service.handle_transfer(
                conference_name=conference_name,
                agent_phone_number=agent_phone_number,
                customer_call_sid=context.call_sid,
                outbound_number=outbound_number,
            )
        )

        if not conference_result.get("success"):
            outcome_task.cancel()
            timeout_task.cancel()
            for task in (outcome_task, timeout_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            failure_reason = conference_result.get("reason", "unknown_error")
            logger.warning(
                f"[Transfer] MPC setup failed: {failure_reason}. AI continues."
            )
            # Unmute STT so the conversation can continue normally
            await unmute_stt(context, None)
            logger.info(
                f"STT unmuted for call {context.call_sid} after MPC setup failure"
            )
            return {
                "status": "failed",
                "reason": failure_reason,
                "message": f"Transfer failed: {failure_reason}. Continuing with AI assistant.",
            }

        logger.info(
            f"[Transfer] MPC transfer initiated: conference={conference_result.get('conference_id')}"
        )

        # Wait for MPC outcome — subscriber consumes whichever fires first
        # (webhook "answered" or our own "unavailable" from _timeout_publish).
        try:
            outcome = await outcome_task
        except asyncio.CancelledError:
            timeout_task.cancel()
            try:
                await timeout_task
            except asyncio.CancelledError:
                pass
            # Unmute STT so the conversation can continue normally
            await unmute_stt(context, None)
            logger.info(
                f"STT unmuted for call {context.call_sid} after subscriber cancellation"
            )
            return {
                "status": "failed",
                "reason": "cancelled",
                "message": "Transfer was cancelled. Continuing with AI assistant.",
            }

        # Cancel the timeout task if the webhook arrived first
        timeout_task.cancel()
        try:
            await timeout_task
        except asyncio.CancelledError:
            pass

        status = outcome.get("status") if outcome else "unavailable"

        if status == "answered":
            logger.info(
                f"[Transfer] Agent answered for call {context.call_sid}. "
                f"Ending AI conversation."
            )

            # Update lead metadata
            transfer_meta = {
                "status": "success",
                "conference_id": conference_result.get("conference_id"),
                "agent_phone_number": agent_phone_number,
            }
            if context.lead and hasattr(context.lead, "metaData"):
                if context.lead.metaData is None:
                    context.lead.metaData = {}
                context.lead.metaData["transfer"] = transfer_meta

            # For Plivo: suppress the serializer's auto hang-up before ending
            # the conversation. When end_conversation pushes EndFrame through
            # the pipeline the Plivo serializer would normally call _hang_up_call(),
            # which drops the caller. Setting _hangup_attempted=True tells the
            # serializer that a hang-up has already been handled so it skips.
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
                "message": "Successfully transferred to human agent",
            }

        # "unavailable" — agent didn't answer or hung up immediately
        logger.warning(
            f"[Transfer] Agent unavailable for call {context.call_sid}. AI continues."
        )
        # Unmute STT so the conversation can continue normally
        await unmute_stt(context, None)
        logger.info(f"STT unmuted for call {context.call_sid} after agent unavailable")
        return {
            "status": "failed",
            "reason": "unavailable",
            "message": "Transfer failed: agent did not answer. Continuing with AI assistant.",
        }

    except Exception as e:
        logger.error(f"Transfer exception: {str(e)}", exc_info=True)
        # Unmute STT so the conversation can continue normally
        try:
            await unmute_stt(context, None)
            logger.info(
                f"STT unmuted for call {context.call_sid} after transfer exception"
            )
        except Exception as unmute_err:
            logger.warning(f"Failed to unmute STT after exception: {unmute_err}")
        return {
            "status": "failed",
            "reason": "exception",
            "message": "Transfer failed due to error. Continuing with AI assistant.",
            "error": str(e),
        }
