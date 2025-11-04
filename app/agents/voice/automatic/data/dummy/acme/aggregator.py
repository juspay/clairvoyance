"""
ACME Data Aggregator
Generic aggregator function with mod 31 logic for time-based data aggregation
"""

import copy
from datetime import datetime
from typing import Optional, List, Any, Dict


def parse_date_to_day_of_year(date_str: str) -> int:
    """Parse date string and return day of year"""
    if not date_str:
        return datetime.now().timetuple().tm_yday

    try:
        # Handle different date formats
        if date_str.endswith('Z'):
            date_str = date_str[:-1] + '+00:00'

        formats = ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]

        for fmt in formats:
            try:
                parsed_date = datetime.strptime(date_str, fmt)
                return parsed_date.timetuple().tm_yday
            except ValueError:
                continue

        # Fallback to current date
        return datetime.now().timetuple().tm_yday

    except Exception:
        return datetime.now().timetuple().tm_yday


def get_data_indices(start_time: Optional[str] = None, end_time: Optional[str] = None) -> List[int]:
    """
    Get data indices using mod 31 logic

    Args:
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        List of indices to aggregate
    """
    # Default to today if no dates provided
    start_day = parse_date_to_day_of_year(start_time) if start_time else datetime.now().timetuple().tm_yday
    end_day = parse_date_to_day_of_year(end_time) if end_time else datetime.now().timetuple().tm_yday

    # Apply mod 31
    start_idx = start_day % 31
    end_idx = end_day % 31

    indices = []

    if start_idx <= end_idx:
        # Simple case: start to end within same cycle
        for i in range(start_idx, end_idx + 1):
            indices.append(i)
    else:
        # Wrap around case: start to end of cycle + beginning to end
        for i in range(start_idx, 31):
            indices.append(i)
        for i in range(0, end_idx + 1):
            indices.append(i)

    return indices


def aggregate_data(
    data_array: List[Dict[str, Any]],
    data_path: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    aggregation_type: str = "sum"
) -> Any:
    """
    Generic aggregator function

    Args:
        data_array: The data array (ACME_BREEZE_DATA or ACME_JUSPAY_DATA)
        data_path: Path to the data field (e.g., "businessTotalSalesBreakdown.value.value")
        start_time: Start date string (optional)
        end_time: End date string (optional)
        aggregation_type: "sum", "average", "first", or "complete"

    Returns:
        Aggregated value or complete data structure
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Handle single day case
    if len(indices) == 1:
        index = indices[0]
        if index < len(data_array):
            if aggregation_type == "complete":
                return data_array[index]
            else:
                return get_nested_value(data_array[index], data_path)
        return None

    # Multi-day aggregation
    if aggregation_type == "complete":
        # Return list of complete data structures
        return [data_array[i] for i in indices if i < len(data_array)]

    # Aggregate numeric values
    values = []
    for index in indices:
        if index < len(data_array):
            value = get_nested_value(data_array[index], data_path)
            if value is not None and isinstance(value, (int, float)):
                values.append(value)

    if not values:
        return None

    if aggregation_type == "sum":
        return sum(values)
    elif aggregation_type == "average":
        return sum(values) / len(values)
    elif aggregation_type == "first":
        return values[0]
    else:
        return sum(values)  # Default to sum


def get_nested_value(data: Dict[str, Any], path: str) -> Any:
    """
    Get nested value from dictionary using dot notation path

    Args:
        data: Dictionary to search
        path: Dot notation path (e.g., "businessTotalSalesBreakdown.value.value")

    Returns:
        Value at the path or None if not found
    """
    try:
        keys = path.split('.')
        current = data

        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None

        return current
    except Exception:
        return None


def aggregate_complete_structure(
    data_array: List[Dict[str, Any]],
    structure_path: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Aggregate complete data structure (for returning full tool responses)

    Args:
        data_array: The data array (ACME_BREEZE_DATA or ACME_JUSPAY_DATA)
        structure_path: Path to the structure (e.g., "businessTotalSalesBreakdown")
        start_time: Start date string (optional)
        end_time: End date string (optional)

    Returns:
        Complete aggregated structure ready for tool response
    """
    indices = get_data_indices(start_time, end_time)

    if not indices:
        return None

    # Single day - return complete structure
    if len(indices) == 1:
        index = indices[0]
        if index < len(data_array):
            return get_nested_value(data_array[index], structure_path)
        return None

    # Multi-day - aggregate the main value but return complete structure from first day
    # This maintains the tool response format while aggregating the key metric
    first_index = indices[0]
    if first_index >= len(data_array):
        return None

    base_structure = get_nested_value(data_array[first_index], structure_path)
    if not base_structure:
        return None

    # Deep copy to avoid modifying original data
    result = copy.deepcopy(base_structure)

    # Aggregate the main value
    main_value_path = "value.value"  # Most structures have this pattern
    aggregated_value = aggregate_data(data_array, f"{structure_path}.{main_value_path}", start_time, end_time, "sum")

    if aggregated_value is not None and "value" in result and "value" in result["value"]:
        result["value"]["value"] = aggregated_value

    return result