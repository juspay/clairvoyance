"""
Main database accessor module.
This module exports all database accessor functions.
"""

from .breeze_buddy.call_data import (
    create_call_data,
    get_call_data_by_id,
    get_call_data_by_call_id,
    update_call_data_status,
    update_call_data_outcome,
    update_call_data_call_id,
    complete_call_data_update,
    get_call_data_by_status,
    get_call_data_by_provider,
    get_call_data_by_requested_by,
    delete_call_data,
    get_all_call_data,
)
from .breeze_buddy.outbound_number import (
    create_outbound_number,
    get_outbound_number_by_id,
    update_outbound_number_status,
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
)

__all__ = [
    "create_call_data",
    "get_call_data_by_id",
    "get_call_data_by_call_id",
    "update_call_data_status",
    "update_call_data_outcome",
    "update_call_data_call_id",
    "complete_call_data_update",
    "get_call_data_by_status",
    "get_call_data_by_provider",
    "get_call_data_by_requested_by",
    "delete_call_data",
    "get_all_call_data",
    "create_outbound_number",
    "get_outbound_number_by_id",
    "update_outbound_number_status",
    "disable_outbound_number",
    "get_all_outbound_numbers",
    "get_outbound_number_based_on_status_and_provider",
    "create_call_execution_config",
    "get_call_execution_config_by_merchant_id",
    "create_lead_call_tracker",
]
