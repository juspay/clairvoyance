"""Inbound call handling for telephony voice agents."""

import uuid
from datetime import datetime
from typing import Dict, Optional, Tuple

from pipecat.runner.types import CallData

from app.core.logger import logger
from app.database.accessor import get_lead_by_call_id
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    create_lead_call_tracker,
)
from app.database.accessor.breeze_buddy.outbound_number import (
    get_outbound_number_by_number,
)
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id,
    get_template_by_outbound_number_id,
)
from app.schemas import CallDirection, CallProvider, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker


async def handle_inbound_call(
    call_sid: str,
    call_data: CallData,
    call_initiated_time: datetime,
    provider: str,
    url_query_params: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[LeadCallTracker], Optional[str]]:
    """Handle an inbound call by creating a lead on-the-fly.

    Args:
        call_sid: The call SID
        call_data: Call data from the telephony provider
        call_initiated_time: When the call was initiated
        provider: The telephony provider (twilio/exotel/plivo)
        url_query_params: Optional URL query params (for Plivo inbound)

    Returns:
        Tuple of (lead, error_reason). If successful, lead is set and error_reason is None.
        If failed, lead is None and error_reason contains the failure reason.
    """
    # Inbound calls supported for Exotel and Plivo
    if provider not in (CallProvider.EXOTEL, CallProvider.PLIVO):
        logger.warning(
            f"Inbound calls not supported for {provider}. call_sid: {call_sid}"
        )
        return None, "Inbound calls not supported"

    # Defensive guard: lead may have been created in the answer handler
    existing_lead = await get_lead_by_call_id(call_sid)
    if existing_lead:
        logger.info(
            f"Lead already exists for call_sid: {call_sid}, lead_id: {existing_lead.id}"
        )
        return existing_lead, None

    logger.info(f"No lead found for call_sid: {call_sid} - treating as inbound call")

    # Get the "to" and "from" numbers from call data or URL query params (for Plivo)
    url_params = url_query_params or {}
    to_number = call_data.get("to") or url_params.get("to_number")
    from_number = call_data.get("from") or url_params.get("from_number", "unknown")

    if not to_number:
        logger.error("Could not determine 'to' number from call data for inbound call")
        return None, "Missing 'to' number"

    logger.info(f"Inbound call to: {to_number}, from: {from_number}")

    # Look up outbound number by phone number
    outbound_number = await get_outbound_number_by_number(to_number)
    if not outbound_number:
        logger.error(f"No outbound number found for to_number: {to_number}")
        return None, "Outbound number not found"

    # Look up template by outbound_number_id (only inbound-enabled templates)
    template = await get_template_by_outbound_number_id(
        outbound_number.id, enable_inbound_only=True
    )
    if not template:
        logger.error(
            f"No inbound-enabled template found for outbound_number_id: {outbound_number.id}"
        )
        return None, "No inbound template configured"

    # Create lead on-the-fly for inbound call
    lead_id = str(uuid.uuid4())
    lead = await create_lead_call_tracker(
        id=lead_id,
        reseller_id=template.reseller_id,
        template=template.name,
        template_id=str(template.id),
        merchant_id=template.merchant_id,
        next_attempt_at=None,
        payload={"customer_mobile_number": from_number},
        call_initiated_time=call_initiated_time,
        status=LeadCallStatus.PROCESSING,
        call_id=call_sid,
        outbound_number_id=outbound_number.id,
        call_direction=CallDirection.INBOUND,
    )

    if not lead:
        logger.error("Failed to create lead for inbound call")
        return None, "Failed to create lead"

    logger.info(f"Created lead for inbound call: {lead.id}, call_sid: {call_sid}")
    return lead, None


async def create_lead_from_template_id(
    template_id: str,
    call_sid: str,
    call_data: CallData,
    call_initiated_time: datetime,
    url_query_params: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[LeadCallTracker], Optional[str]]:
    """Create a lead for inbound call using a specific template_id.

    Used when template is selected via IVR or passed as query param.

    Args:
        template_id: The template ID (from IVR selection or query param)
        call_sid: The call SID
        call_data: Call data from the telephony provider
        call_initiated_time: When the call was initiated
        url_query_params: Optional URL query params (for Plivo inbound)

    Returns:
        Tuple of (lead, error_reason). If successful, lead is set and error_reason is None.
        If failed, lead is None and error_reason contains the failure reason.
    """
    logger.info(f"Creating lead from template_id: {template_id}")

    # Defensive guard: lead may have been created in the answer handler
    existing_lead = await get_lead_by_call_id(call_sid)
    if existing_lead:
        logger.info(
            f"Lead already exists for call_sid: {call_sid}, lead_id: {existing_lead.id}"
        )
        return existing_lead, None

    # Load template by ID
    template = await get_template_by_id(template_id)
    if not template:
        logger.error(f"Template not found for template_id: {template_id}")
        return None, "Template not found"

    # Get from_number from call_data or URL query params (for Plivo)
    start_data = call_data.get("start", {})
    custom_params = call_data.get("custom_parameters") or start_data.get(
        "custom_parameters", {}
    )
    url_params = url_query_params or {}
    from_number = (
        start_data.get("from")
        or call_data.get("from")
        or custom_params.get("from_number")
        or url_params.get("from_number", "unknown")
    )

    # Create lead with the selected template
    lead_id = str(uuid.uuid4())
    lead = await create_lead_call_tracker(
        id=lead_id,
        reseller_id=template.reseller_id,
        template=template.name,
        template_id=str(template.id),
        merchant_id=template.merchant_id,
        next_attempt_at=None,
        payload={"customer_mobile_number": from_number},
        call_initiated_time=call_initiated_time,
        status=LeadCallStatus.PROCESSING,
        call_id=call_sid,
        outbound_number_id=template.outbound_number_id,
        call_direction=CallDirection.INBOUND,
    )

    if not lead:
        logger.error("Failed to create lead for inbound call with template_id")
        return None, "Failed to create lead"

    logger.info(f"Created lead for inbound call: {lead.id}, template: {template.name}")
    return lead, None
