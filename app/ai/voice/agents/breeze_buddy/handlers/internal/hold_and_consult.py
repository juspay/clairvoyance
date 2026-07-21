"""
Hold & Consultative Transfer Handler

Places the inbound caller on hold (mute STT + play music), initiates an
outbound call to a third party, waits for the outbound conversation to
complete via Redis pub/sub, then returns the result to the LLM so it can
resume the original conversation with context from the outbound call.

Unlike warm transfer (which bridges two humans), hold & consult returns
the AI agent to the original caller.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import (
    mute_stt,
    unmute_stt,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    HoldTransferConfig,
)
from app.ai.voice.agents.breeze_buddy.utils.common import validate_payload
from app.ai.voice.agents.breeze_buddy.utils.hold_transfer import (
    subscribe_and_wait,
)
from app.core.logger import logger
from app.database.accessor import get_telephony_number_by_id
from app.database.accessor.breeze_buddy.hold_transfer import (
    get_template_summary_by_outbound_number_id,
)
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    create_lead_call_tracker,
    update_lead_call_id_by_id,
    update_lead_request_id,
)
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus

# Channel name pattern for Redis pub/sub
_CHANNEL_PREFIX = "hold_transfer:result"


async def hold_and_consult(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Hold & consultative transfer handler.

    Args:
        context: Handler context with bot state access.
        args: LLM function arguments — expects "outbound_payload" dict with
              at minimum "customer_mobile_number". All fields in outbound_payload
              are stored as-is in the outbound lead's payload alongside internal
              _hold_transfer_* keys. config.phone_number overrides
              customer_mobile_number if set.
        transition_to: Not used.

    Returns:
        Dict with status, summary/message for the LLM.
    """
    call_sid = context.call_sid
    args_payload: Dict[str, Any] = args.get("outbound_payload") or {}
    logger.info(
        f"[hold_and_consult] Called for call {call_sid}, "
        f"has_customer_mobile_number={'customer_mobile_number' in args_payload}"
    )

    # ── 1. Validate configuration ──────────────────────────────────────
    configurations = getattr(context.bot, "configurations", None)
    hold_config: Optional[HoldTransferConfig] = getattr(
        configurations, "hold_transfer", None
    )
    if not hold_config:
        logger.error(f"[hold_and_consult] No hold_transfer config for call {call_sid}")
        return {
            "status": "error",
            "message": "Hold transfer is not configured for this assistant.",
        }

    # ── 2. Resolve phone number (config > LLM outbound_payload) ───────────
    phone_number = hold_config.phone_number  # config takes priority
    if not phone_number:
        phone_number = args_payload.get("customer_mobile_number")
    if not phone_number:
        return {
            "status": "error",
            "message": (
                "customer_mobile_number is required. Set it in hold_transfer config "
                "or pass it inside outbound_payload."
            ),
        }
    logger.info(
        f"[hold_and_consult] Phone number resolved for call {call_sid}: "
        f"source={'config' if hold_config.phone_number else 'outbound_payload'}"
    )

    # ── 3. Lookup outbound number ──────────────────────────────────────
    telephony_number_record = await get_telephony_number_by_id(
        hold_config.outbound_number_id
    )
    if not telephony_number_record:
        logger.error(
            f"[hold_and_consult] Outbound number not found: "
            f"{hold_config.outbound_number_id}"
        )
        return {
            "status": "error",
            "message": "Outbound number not found.",
        }
    telephony_number = telephony_number_record.number

    # ── 4. Lookup outbound template ────────────────────────────────────
    outbound_template = await get_template_summary_by_outbound_number_id(
        hold_config.outbound_number_id
    )
    if not outbound_template:
        logger.error(
            f"[hold_and_consult] No template linked to outbound_number_id "
            f"{hold_config.outbound_number_id}"
        )
        return {
            "status": "error",
            "message": "No template configured for the outbound number.",
        }

    # ── 5. Check telephony service availability ────────────────────────
    if not hasattr(context, "telephony_service") or context.telephony_service is None:
        logger.error(f"[hold_and_consult] No telephony_service for call {call_sid}")
        return {
            "status": "error",
            "message": "Telephony service not available.",
        }

    # ── 6. Timeout warning ─────────────────────────────────────────────
    if hold_config.hold_timeout_seconds > 30:
        logger.warning(
            f"[hold_and_consult] hold_timeout_seconds="
            f"{hold_config.hold_timeout_seconds}s for call {call_sid}. "
            f"Ensure the template's llm_configuration has "
            f"function_call_timeout_secs >= "
            f"{hold_config.hold_timeout_seconds}s "
            f"or Pipecat will kill this handler early (default is 10s)."
        )

    # ── 7. Build pub/sub channel & subscribe BEFORE making the call ────
    pub_channel = f"{_CHANNEL_PREFIX}:{call_sid}"
    subscribe_task = asyncio.create_task(
        subscribe_and_wait(pub_channel, hold_config.hold_timeout_seconds)
    )
    logger.info(f"[hold_and_consult] Subscribed to '{pub_channel}' for call {call_sid}")

    # ── 8. Create outbound lead with hold-transfer metadata ────────────
    outbound_payload: Dict[str, Any] = {}

    # LLM-provided outbound_payload fields
    outbound_payload.update(args_payload)

    # Internal keys always win — injected by system, not LLM
    outbound_payload["_hold_transfer_pub_channel"] = pub_channel
    if hold_config.summarize:
        outbound_payload["_hold_transfer_summarize"] = True

    # Validate constructed payload against outbound template's expected schema (warn only)
    expected_schema = outbound_template.get("expected_payload_schema")
    if expected_schema:
        is_valid, validation_errors = validate_payload(
            outbound_payload, expected_schema
        )
        if not is_valid:
            logger.warning(
                f"[hold_and_consult] Outbound payload does not satisfy "
                f"template '{outbound_template['name']}' expected_payload_schema "
                f"(call {call_sid}). Proceeding anyway. Errors: {validation_errors}"
            )

    # Use inbound lead ID as request_id for cross-leg traceability
    inbound_lead_id = context.lead.id if context.lead else None

    outbound_lead_id = str(uuid.uuid4())
    try:
        outbound_lead = await create_lead_call_tracker(
            id=outbound_lead_id,
            reseller_id=outbound_template["reseller_id"],
            template=outbound_template["name"],
            template_id=str(outbound_template["id"]),
            merchant_id=outbound_template["merchant_id"],
            next_attempt_at=None,
            payload=outbound_payload,
            call_initiated_time=datetime.now(timezone.utc),
            status=LeadCallStatus.PROCESSING,
            call_id=None,  # populated after make_call returns
            outbound_number_id=hold_config.outbound_number_id,
            call_direction=CallDirection.OUTBOUND,
            execution_mode=ExecutionMode.HOLD_TRANSFER,
            request_id=inbound_lead_id,
        )
        if not outbound_lead:
            subscribe_task.cancel()
            logger.error(
                f"[hold_and_consult] Failed to create outbound lead "
                f"for call {call_sid}"
            )
            return {
                "status": "error",
                "message": "Failed to create outbound lead.",
            }

        # Link inbound lead → outbound lead by storing the outbound lead UUID
        # in the inbound lead's request_id. The reverse link (outbound → inbound)
        # is already set via request_id=inbound_lead_id above.
        if inbound_lead_id:
            await update_lead_request_id(str(inbound_lead_id), outbound_lead_id)
    except Exception as e:
        subscribe_task.cancel()
        logger.error(
            f"[hold_and_consult] Exception creating outbound lead: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "message": f"Failed to create outbound lead: {str(e)}",
        }

    # ── 9. Mute STT + play hold music, make outbound call ──────────────
    result = None
    try:
        await mute_stt(context, {})
        await context.manage_audio_mixer(
            enable=True,
            settings={
                "sound": hold_config.hold_music.value,
                "volume": hold_config.hold_music_volume,
            },
        )
        logger.info(
            f"[hold_and_consult] STT muted + hold music started " f"for call {call_sid}"
        )

        # ── 10. Initiate outbound call (blocking SDK → worker thread) ─────
        loop = asyncio.get_event_loop()
        call_result = await loop.run_in_executor(
            None,
            lambda: context.telephony_service.make_call(
                customer_mobile_number=phone_number,
                telephony_number=telephony_number,
                reseller_id=outbound_template["reseller_id"],
                template_name=outbound_template["name"],
            ),
        )
        if not call_result or not call_result.get("sid"):
            subscribe_task.cancel()
            logger.error(
                f"[hold_and_consult] make_call failed for call {call_sid}: "
                f"{call_result}"
            )
            return {
                "status": "error",
                "message": "Failed to initiate outbound call.",
            }

        outbound_call_sid = str(call_result["sid"])
        logger.info(
            f"[hold_and_consult] Outbound call initiated: "
            f"{outbound_call_sid} from inbound {call_sid}"
        )

        # ── 11. Update outbound lead with actual call_id ───────────────
        # Treat failure as terminal: without the call_id persisted, telephony
        # callbacks can't correlate back to this lead tracker.
        try:
            await update_lead_call_id_by_id(outbound_lead_id, outbound_call_sid)
            logger.info(
                f"[hold_and_consult] Updated lead {outbound_lead_id} "
                f"with call_id {outbound_call_sid}"
            )
        except Exception as update_err:
            subscribe_task.cancel()
            logger.error(
                f"[hold_and_consult] Failed to update call_id for "
                f"lead {outbound_lead_id}: {update_err}",
                exc_info=True,
            )
            return {
                "status": "error",
                "message": "Failed to track outbound call. The transfer has been aborted.",
            }

        # ── 12. Wait for pub/sub result ────────────────────────────────
        result = await subscribe_task
    except asyncio.CancelledError:
        logger.warning(
            f"[hold_and_consult] Cancelled while waiting for call {call_sid}"
        )
    except Exception as e:
        logger.error(
            f"[hold_and_consult] Exception during hold for call {call_sid}: {e}",
            exc_info=True,
        )
    finally:
        # Always cancel subscribe_task if still running
        if not subscribe_task.done():
            subscribe_task.cancel()
            try:
                await subscribe_task
            except asyncio.CancelledError:
                pass

        # Always restore audio + STT
        try:
            await context.manage_audio_mixer(enable=False)
        except Exception as e:
            logger.error(
                f"[hold_and_consult] Error stopping music " f"for call {call_sid}: {e}"
            )
        try:
            await unmute_stt(context, {})
        except Exception as e:
            logger.error(
                f"[hold_and_consult] Error unmuting STT for call {call_sid}: {e}"
            )

    # ── 13. Build result for LLM ───────────────────────────────────────
    if result is None:
        logger.warning(
            f"[hold_and_consult] Timeout or error waiting for outbound "
            f"result for call {call_sid}"
        )
        return {
            "status": "timeout",
            "message": (
                "I wasn't able to reach the other party within the "
                "expected time. Let me help you with something else."
            ),
        }

    status = result.get("status", "unknown")
    logger.info(
        f"[hold_and_consult] Returning result for call {call_sid}: "
        f"status={status}, has_summary={'summary' in result}, "
        f"has_transcription={'transcription' in result}"
    )

    if status in ("success", "incomplete"):
        response = {
            "status": status,
        }
        if result.get("summary"):
            response["summary"] = result["summary"]
        else:
            response["transcription"] = result.get("transcription", [])
        return response
    else:
        # no_answer, busy, failed, etc.
        return {
            "status": status,
            "message": f"The outbound call ended with status: {status}.",
        }
