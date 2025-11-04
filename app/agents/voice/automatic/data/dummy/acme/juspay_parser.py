"""
ACME Juspay Data Parser
Handles all Juspay analytics data using the generic aggregator
"""

from typing import Any, Dict, List, Optional

try:
    from .aggregator import aggregate_data, get_data_indices
    from .juspay_data import ACME_JUSPAY_DATA
except ImportError:
    # Fallback for direct execution
    from aggregator import aggregate_data, get_data_indices
    from juspay_data import ACME_JUSPAY_DATA


def get_success_rate(
    start_time: Optional[str] = None, end_time: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Get Juspay success rate data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Aggregated success rate data
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_JUSPAY_DATA):
            return ACME_JUSPAY_DATA[index]["overall_success_rate_data"]
        return None

    # Multi-day aggregation
    success_rates = []
    total_attempts = 0
    successful_transactions = 0
    failed_transactions = 0

    for index in indices:
        if index < len(ACME_JUSPAY_DATA):
            data = ACME_JUSPAY_DATA[index]["overall_success_rate_data"]
            success_rates.append(data["success_rate"])
            total_attempts += data["total_attempts"]
            successful_transactions += data["successful_transactions"]
            failed_transactions += data["failed_transactions"]

    if not success_rates:
        return None

    return {
        "success_rate": round(sum(success_rates) / len(success_rates), 2),
        "total_attempts": total_attempts,
        "successful_transactions": successful_transactions,
        "failed_transactions": failed_transactions,
        "processing_time_avg": 0,  # Would need additional aggregation logic
        "retry_success_rate": 0,
        "peak_hour_sr": 0,
        "off_peak_sr": 0,
    }


def get_payment_method_sr(
    start_time: Optional[str] = None, end_time: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Get payment method success rates with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Aggregated payment method success rates
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_JUSPAY_DATA):
            return ACME_JUSPAY_DATA[index]["payment_method_success_rates"]
        return None

    # Multi-day aggregation
    method_totals = {}

    for index in indices:
        if index < len(ACME_JUSPAY_DATA):
            for method in ACME_JUSPAY_DATA[index]["payment_method_success_rates"]:
                pm_type = method["payment_method_type"]
                if pm_type not in method_totals:
                    method_totals[pm_type] = {
                        "payment_method_type": pm_type,
                        "success_rates": [],
                        "total_attempts": 0,
                        "successful": 0,
                        "failed": 0,
                    }

                method_totals[pm_type]["success_rates"].append(method["success_rate"])
                method_totals[pm_type]["total_attempts"] += method["total_attempts"]
                method_totals[pm_type]["successful"] += method["successful"]
                method_totals[pm_type]["failed"] += method["failed"]

    # Build result
    result = []
    for pm_type, data in method_totals.items():
        result.append(
            {
                "payment_method_type": pm_type,
                "success_rate": (
                    round(sum(data["success_rates"]) / len(data["success_rates"]), 1)
                    if data["success_rates"]
                    else 0
                ),
                "total_attempts": data["total_attempts"],
                "successful": data["successful"],
                "failed": data["failed"],
                "avg_processing_time": 0,  # Would need additional aggregation
                "failure_reasons": [],
            }
        )

    return result


def get_success_transactional_data(
    start_time: Optional[str] = None, end_time: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Get success transactional data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Aggregated success volume data
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_JUSPAY_DATA):
            return ACME_JUSPAY_DATA[index]["success_volume_by_payment_method"]
        return None

    # Multi-day aggregation
    method_totals = {}

    for index in indices:
        if index < len(ACME_JUSPAY_DATA):
            for method in ACME_JUSPAY_DATA[index]["success_volume_by_payment_method"]:
                pm_type = method["payment_method_type"]
                if pm_type not in method_totals:
                    method_totals[pm_type] = {
                        "payment_method_type": pm_type,
                        "transaction_count": 0,
                        "peak_hour_volume": 0,
                    }

                method_totals[pm_type]["transaction_count"] += method.get(
                    "transaction_count", 0
                )
                method_totals[pm_type]["peak_hour_volume"] += method.get(
                    "peak_hour_volume", 0
                )

    # Calculate percentages
    total_transactions = sum(
        data["transaction_count"] for data in method_totals.values()
    )

    result = []
    for pm_type, data in method_totals.items():
        percentage = (
            (data["transaction_count"] / total_transactions * 100)
            if total_transactions > 0
            else 0
        )
        result.append(
            {
                "payment_method_type": pm_type,
                "transaction_count": data["transaction_count"],
                "percentage": round(percentage, 1),
                "peak_hour_volume": data["peak_hour_volume"],
            }
        )

    return result


def get_failure_transactional_data(
    start_time: Optional[str] = None, end_time: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Get failure transactional data with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Aggregated failure data
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_JUSPAY_DATA):
            return ACME_JUSPAY_DATA[index]["failure_details"]
        return None

    # Multi-day aggregation would follow similar pattern
    # For now, return first day's data as placeholder
    if indices and indices[0] < len(ACME_JUSPAY_DATA):
        return ACME_JUSPAY_DATA[indices[0]]["failure_details"]

    return None


def get_gmv_by_payment_method(
    start_time: Optional[str] = None, end_time: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Get GMV by payment method with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Aggregated GMV data
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_JUSPAY_DATA):
            return ACME_JUSPAY_DATA[index]["gmv_by_payment_method"]
        return None

    # Multi-day aggregation
    method_totals = {}

    for index in indices:
        if index < len(ACME_JUSPAY_DATA):
            for method in ACME_JUSPAY_DATA[index]["gmv_by_payment_method"]:
                pm_type = method["payment_method_type"]
                if pm_type not in method_totals:
                    method_totals[pm_type] = {
                        "payment_method_type": pm_type,
                        "gmv": 0,
                    }

                method_totals[pm_type]["gmv"] += method.get("gmv", 0)

    # Calculate percentages
    total_gmv = sum(data["gmv"] for data in method_totals.values())

    result = []
    for pm_type, data in method_totals.items():
        percentage = (data["gmv"] / total_gmv * 100) if total_gmv > 0 else 0
        result.append(
            {
                "payment_method_type": pm_type,
                "gmv": data["gmv"],
                "percentage": round(percentage, 1),
            }
        )

    return result


def get_average_ticket_size(
    start_time: Optional[str] = None, end_time: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Get average ticket size with aggregation

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Aggregated ticket size data
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day
    if len(indices) == 1:
        index = indices[0]
        if index < len(ACME_JUSPAY_DATA):
            return ACME_JUSPAY_DATA[index]["average_ticket_size_by_payment_method"]
        return None

    # Multi-day aggregation would average ticket sizes
    # For now, return first day's data as placeholder
    if indices and indices[0] < len(ACME_JUSPAY_DATA):
        return ACME_JUSPAY_DATA[indices[0]]["average_ticket_size_by_payment_method"]

    return None
