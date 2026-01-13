"""
Main database accessor module.
This module exports all database accessor functions.
"""

from .breeze_buddy.analytics import get_langfuse_scores_by_merchant
from .breeze_buddy.call_execution_config import (
    calling_activation_for_merchant,
    create_call_execution_config,
    delete_call_execution_config,
    get_all_call_execution_configs,
    get_call_execution_config_by_id,
    get_call_execution_config_by_merchant_id,
    update_call_execution_config,
)
from .breeze_buddy.lead_call_tracker import (
    acquire_lock_on_lead_by_id,
    create_lead_call_tracker,
    get_all_lead_call_trackers,
    get_lead_based_analytics,
    get_lead_by_call_id,
    get_lead_by_id,
    get_lead_call_trackers_count,
    get_leads_based_on_status_and_next_attempt,
    get_leads_by_status_and_time_before,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
    update_lead_call_details,
    update_lead_call_initiated_time,
    update_lead_call_recording_url,
)
from .breeze_buddy.outbound_number import (
    create_outbound_number,
    disable_outbound_number,
    get_all_outbound_numbers,
    get_all_outbound_numbers_with_call_count,
    get_outbound_number_based_on_status_and_provider,
    get_outbound_number_by_id,
    update_outbound_number_channels,
    update_outbound_number_status,
)
from .breeze_buddy.template import (
    create_template,
    get_template_by_merchant,
)

__all__ = [
    "create_template",
    "get_template_by_merchant",
    "create_outbound_number",
    "get_outbound_number_by_id",
    "update_outbound_number_status",
    "update_outbound_number_channels",
    "disable_outbound_number",
    "get_all_outbound_numbers",
    "get_all_outbound_numbers_with_call_count",
    "get_outbound_number_based_on_status_and_provider",
    "create_call_execution_config",
    "get_call_execution_config_by_id",
    "get_call_execution_config_by_merchant_id",
    "get_all_call_execution_configs",
    "update_call_execution_config",
    "delete_call_execution_config",
    "calling_activation_for_merchant",
    "create_lead_call_tracker",
    "get_leads_based_on_status_and_next_attempt",
    "acquire_lock_on_lead_by_id",
    "release_lock_on_lead_by_id",
    "update_lead_call_details",
    "get_lead_by_call_id",
    "get_lead_by_id",
    "update_lead_call_completion_details",
    "update_lead_call_initiated_time",
    "update_lead_call_recording_url",
    "get_all_lead_call_trackers",
    "get_langfuse_scores_by_merchant",
    "get_lead_based_analytics",
    "get_lead_call_trackers_count",
    "get_leads_by_status_and_time_before",
]
