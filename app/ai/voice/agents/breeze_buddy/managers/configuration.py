"""
Lead configuration utilities.
Handles retrieval of call execution configs.
"""

from typing import Optional

from app.core.logger import logger
from app.database.accessor import get_call_execution_config_by_merchant_id
from app.schemas import CallExecutionConfig, LeadCallTracker


async def get_lead_config(lead: LeadCallTracker) -> Optional[CallExecutionConfig]:
    """
    Retrieves the call execution configuration for a given lead.
    """
    configs = await get_call_execution_config_by_merchant_id(
        lead.reseller_id, lead.merchant_id
    )
    if not configs:
        logger.warning(
            f"No call execution config found for reseller: {lead.reseller_id} and shop: {lead.merchant_id}"
        )
        return None

    config = next((c for c in configs if c.template == lead.template), None)
    if not config:
        logger.warning(f"No call execution config found for template: {lead.template}")
    return config
