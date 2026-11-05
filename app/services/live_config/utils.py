"""
Utility functions for DevCycle feature flag processing.

Contains helper functions for data type conversion, key normalization,
and DevCycle value processing.
"""

import os
from typing import Any, Dict, Optional


def normalize_key(key: str) -> str:
    """Normalize key to uppercase with underscores"""
    return key.upper().replace("-", "_").replace(".", "_")


def convert_type(value: Any, target_type: type) -> Any:
    """Convert value to target type with fallback handling"""
    if target_type == bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() == "true"
    elif target_type == int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    elif target_type == float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    elif target_type == list:
        if isinstance(value, list):
            return value
        str_value = str(value) if value else ""
        return [item.strip() for item in str_value.split(",") if item.strip()]
    else:  # str
        return str(value)


def get_env_value(key: str, return_type: type) -> Optional[Any]:
    """Get and convert environment variable value"""
    env_value = os.environ.get(key)
    if env_value is None:
        return None

    converted = convert_type(env_value, return_type)
    return converted if converted is not None else env_value


def process_devcycle_value(value: Any, var_type: str) -> Any:
    """Process DevCycle value based on its type"""
    if var_type == "Boolean" and isinstance(value, str):
        return value.lower() == "true"
    elif var_type == "String" and isinstance(value, bool):
        return str(value).lower()
    return value


def build_variable_mapping(variables: list) -> Dict[str, Dict[str, str]]:
    """Build mapping of variable IDs to their metadata"""
    mapping = {}
    for variable in variables:
        if isinstance(variable, dict) and "_id" in variable and "key" in variable:
            mapping[variable["_id"]] = {
                "key": variable["key"],
                "type": variable.get("type", "String"),
            }
    return mapping
