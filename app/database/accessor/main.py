"""
Main database accessor module.
This module exports all database accessor functions.
"""

from .call_data import (
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
from .outbound_number import (
    create_outbound_number,
    get_outbound_number_by_id,
    update_outbound_number_status,
    disable_outbound_number,
    get_all_outbound_numbers,
    get_outbound_number_based_on_status_and_provider,
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
]
