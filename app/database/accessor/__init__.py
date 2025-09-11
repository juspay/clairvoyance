"""
Main database accessor module.
This module exports all database accessor functions.
"""

from .breeze_buddy.outbound_number import (
    create_outbound_number,
    get_outbound_number_by_id,
    update_outbound_number_status,
    update_outbound_number_channels,
    disable_outbound_number,
    get_all_outbound_numbers,
    get_outbound_number_based_on_status_and_provider,
)
from .breeze_buddy.call_execution_config import (
    create_call_execution_config,
    get_call_execution_config_by_merchant_id,
)
from .breeze_buddy.lead_call_tracker import (
    create_lead_call_tracker,
    get_leads_based_on_status_and_next_attempt,
    update_lead_call_details,
    get_lead_by_call_id,
    update_lead_call_completion_details,
)

__all__ = [
    "create_outbound_number",
    "get_outbound_number_by_id",
    "update_outbound_number_status",
    "update_outbound_number_channels",
    "disable_outbound_number",
    "get_all_outbound_numbers",
    "get_outbound_number_based_on_status_and_provider",
    "create_call_execution_config",
    "get_call_execution_config_by_merchant_id",
    "create_lead_call_tracker",
    "get_leads_based_on_status_and_next_attempt",
    "update_lead_call_details",
    "get_lead_by_call_id",
    "update_lead_call_completion_details",
]
