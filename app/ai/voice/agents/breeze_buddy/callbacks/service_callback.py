import json
from datetime import datetime, timezone

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.common import (
    send_webhook_with_retry,
)
from app.core.logger import logger


async def service_callback(context: TemplateContext, args):
    """
    Handler to send webhook with call summary to the calling service.

    This handler sends the call summary data to the reporting webhook URL.

    Args:
        context: Handler context with bot state access
        args: Function arguments containing outcome and other data

    Returns:
        Empty dict
    """
    logger.debug(f"service_callback called with args: {args}")

    try:
        if context.lead and context.lead.metaData:
            transcript = context.lead.metaData.get("transcription", [])
        else:
            transcript = []
        filtered_transcript = [msg for msg in transcript if msg.get("role") != "system"]
        outcome = context.lead.outcome if context.lead else None
        expected_schema = context.expected_callback_response_schema

        # Calculate call duration
        call_duration = None
        if context.lead and context.lead.call_initiated_time:
            call_initiated_time_utc = context.lead.call_initiated_time.astimezone(
                timezone.utc
            )
            call_duration = (
                datetime.now(timezone.utc) - call_initiated_time_utc
            ).total_seconds()
            logger.debug(
                f"Calculated call duration: {call_duration} seconds for call {context.call_sid}"
            )
        else:
            logger.warning(
                f"Could not calculate call duration for call {context.call_sid} "
                f"(lead: {context.lead is not None}, "
                f"call_initiated_time: {context.lead.call_initiated_time if context.lead else None})"
            )

        # Prepare webhook summary data
        summary_data = {
            "callSid": context.call_sid,
            "outcome": outcome,
            "attemptCount": context.lead.attempt_count + 1 if context.lead else 1,
            "transcription": json.dumps(filtered_transcript, ensure_ascii=False),
            "callDuration": call_duration,
            "orderId": (context.lead.request_id if context.lead else None),
        }

        if expected_schema:
            meta = {}
            if context.lead and context.lead.metaData:
                meta = context.lead.metaData.get("outcome") or {}
            extracted_fields = {}

            # Check for required fields before processing
            missing_required_fields = []
            for field_name, field_config in expected_schema.items():
                # Check if field is required (not marked as optional)
                is_optional = field_config.get("optional", False)

                if not is_optional and field_name not in meta:
                    missing_required_fields.append(field_name)

            # If any required fields are missing, don't send the webhook
            if missing_required_fields:
                logger.warning(
                    f"Skipping webhook send for call {context.call_sid} - "
                    f"missing required fields in metadata: {missing_required_fields}"
                )
                return {}

            # Extract fields that exist in metadata
            for field_name in expected_schema:
                if field_name in meta:
                    extracted_fields[field_name] = meta[field_name]

            summary_data.update(extracted_fields)
            logger.debug(
                f"Extracted schema fields added to summary_data: {extracted_fields}"
            )

        logger.info(
            f"Prepared webhook summary data for call {context.call_sid}: "
            f"outcome={outcome}, order_id={context.lead.request_id if context.lead else None}, "
            f"call_duration={call_duration}s"
        )

        webhook_url = (
            context.lead.payload.get("reporting_webhook_url")
            if context.lead and context.lead.payload
            else None
        )

        if webhook_url:
            logger.info(
                f"Sending webhook to {webhook_url} for call {context.call_sid} "
                f"with outcome {outcome}"
            )
            try:
                success = await send_webhook_with_retry(
                    context.aiohttp_session,
                    webhook_url,
                    summary_data,
                )
                if not success:
                    logger.error(
                        f"Failed to send call summary webhook after all retries for call {context.call_sid}. "
                        f"URL: {webhook_url}"
                    )
                else:
                    logger.info(
                        f"Successfully sent webhook for call {context.call_sid} "
                        f"with outcome: {outcome}"
                    )
            except Exception as e:
                logger.error(
                    f"Error sending webhook for call {context.call_sid}: {e}",
                    exc_info=True,
                )
        else:
            logger.info(
                f"Skipping webhook send for call {context.call_sid} "
                f"(url={'present' if webhook_url else 'missing'}, "
                f"outcome={outcome})"
            )

    except Exception as e:
        logger.error(
            f"Error in service_callback for call {context.call_sid}: {e}",
            exc_info=True,
        )

    return {}
