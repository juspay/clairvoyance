"""Plivo outbound call correlation helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.core.logger import logger
from app.database.accessor import (
    bind_submitted_call_uuid,
    get_lead_by_call_id,
    update_lead_call_details,
)
from app.schemas import LeadCallStatus


def plivo_callback_context(params: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Extract lead binding context from Plivo callback parameters."""
    lead_id = str(params.get("lead_id") or "") or None
    telephony_number_id = str(params.get("telephony_number_id") or "") or None
    return lead_id, telephony_number_id


async def bind_plivo_outbound_call_uuid(
    *,
    lead_id: str | None,
    call_uuid: str | None,
    telephony_number_id: str | None,
) -> bool:
    """Bind Plivo's callback CallUUID to the locked outbound lead.

    Plivo create-call returns RequestUUID, while callbacks carry CallUUID.
    The dispatcher therefore cannot write the live call_id until the first
    callback arrives.
    """
    if not lead_id or not call_uuid or not telephony_number_id:
        return False

    updated = await bind_submitted_call_uuid(
        lead_id,
        call_uuid,
        datetime.now(timezone.utc),
        telephony_number_id,
    )
    if updated:
        logger.info(f"[PlivoCorrelation] Bound CallUUID {call_uuid} to lead {lead_id}")
        return True

    updated = await update_lead_call_details(
        lead_id,
        LeadCallStatus.PROCESSING,
        call_uuid,
        datetime.now(timezone.utc),
        telephony_number_id,
    )
    if updated:
        logger.info(f"[PlivoCorrelation] Bound CallUUID {call_uuid} to lead {lead_id}")
        return True

    existing = await get_lead_by_call_id(call_uuid)
    if existing and existing.id == lead_id:
        return True

    logger.error(
        f"[PlivoCorrelation] Failed to bind CallUUID {call_uuid} to lead {lead_id}"
    )
    return False
