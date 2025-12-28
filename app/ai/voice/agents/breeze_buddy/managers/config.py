"""
Configuration and validation utilities for call management.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.logger import logger
from app.database.accessor import get_call_execution_config_by_merchant_id
from app.schemas import CallExecutionConfig, LeadCallTracker


async def get_lead_config(lead: LeadCallTracker) -> Optional[CallExecutionConfig]:
    """
    Retrieves the call execution configuration for a given lead.
    """
    configs = await get_call_execution_config_by_merchant_id(
        lead.merchant_id, lead.shop_identifier
    )
    if not configs:
        logger.warning(
            f"No call execution config found for merchant: {lead.merchant_id} and shop: {lead.shop_identifier}"
        )
        return None

    config = next((c for c in configs if c.template == lead.template), None)
    if not config:
        logger.warning(f"No call execution config found for template: {lead.template}")
    return config


def is_within_calling_hours(config: CallExecutionConfig) -> bool:
    """
    Checks if the current time is within the allowed calling hours.
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    current_time = datetime.now(IST).time()

    if config.call_start_time <= config.call_end_time:
        # Normal case (e.g., 09:00–17:00)
        return config.call_start_time <= current_time <= config.call_end_time
    else:
        # Overnight case (e.g., 22:00–06:00)
        return (
            current_time >= config.call_start_time
            or current_time <= config.call_end_time
        )
