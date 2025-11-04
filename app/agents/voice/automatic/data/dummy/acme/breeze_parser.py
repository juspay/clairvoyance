"""
ACME Breeze Data Parser
Handles all Breeze analytics data using the generic aggregator
"""

import copy
from typing import Optional, Dict, Any

try:
    from .aggregator import aggregate_complete_structure, get_data_indices, get_nested_value
    from .breeze_data import ACME_BREEZE_DATA
except ImportError:
    # Fallback for direct execution
    from aggregator import aggregate_complete_structure, get_data_indices, get_nested_value
    from breeze_data import ACME_BREEZE_DATA


def get_sales_breakdown(start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get sales breakdown data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Complete sales breakdown structure with aggregated values
    """
    return aggregate_complete_structure(
        data_array=ACME_BREEZE_DATA,
        structure_path="businessTotalSalesBreakdown",
        start_time=start_time,
        end_time=end_time
    )


def get_orders_breakdown(start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get orders breakdown data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Complete orders breakdown structure with aggregated values
    """
    return aggregate_complete_structure(
        data_array=ACME_BREEZE_DATA,
        structure_path="businessTotalOrdersBreakdown",
        start_time=start_time,
        end_time=end_time
    )


def get_conversion_breakdown(start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get conversion breakdown data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Complete conversion breakdown structure with aggregated values
    """
    return aggregate_complete_structure(
        data_array=ACME_BREEZE_DATA,
        structure_path="businessConversionBreakdown",
        start_time=start_time,
        end_time=end_time
    )


def get_payment_success_rate(start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get payment success rate data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Complete payment success rate structure with aggregated values
    """
    return aggregate_complete_structure(
        data_array=ACME_BREEZE_DATA,
        structure_path="paymentSuccessRate",
        start_time=start_time,
        end_time=end_time
    )


def get_average_order_value(start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get average order value data with aggregation

    NOTE: AOV must be calculated as total_revenue / total_orders, not averaged or summed.

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Complete average order value structure with aggregated values
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day - return complete structure
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_BREEZE_DATA):
            return get_nested_value(ACME_BREEZE_DATA[index], "averageOrderValue")
        return None

    # Multi-day - calculate AOV as total_revenue / total_orders
    total_revenue = 0
    total_orders = 0

    for idx in indices:
        if idx < len(ACME_BREEZE_DATA):
            revenue = get_nested_value(ACME_BREEZE_DATA[idx], "businessTotalSalesBreakdown.value.value")
            orders = get_nested_value(ACME_BREEZE_DATA[idx], "businessTotalOrdersBreakdown.value.value")
            if revenue is not None and orders is not None:
                total_revenue += revenue
                total_orders += orders

    # Get base structure from first day
    first_index = indices[0]
    if first_index >= len(ACME_BREEZE_DATA):
        return None

    base_structure = get_nested_value(ACME_BREEZE_DATA[first_index], "averageOrderValue")
    if not base_structure:
        return None

    # Deep copy and update with calculated AOV
    result = copy.deepcopy(base_structure)
    calculated_aov = total_revenue / total_orders if total_orders > 0 else 0

    if "value" in result and "value" in result["value"]:
        result["value"]["value"] = calculated_aov

    return result