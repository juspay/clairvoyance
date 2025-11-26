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


async def process_feature_variables(
    feature: dict, variable_mapping: Dict[str, Dict[str, str]], flag_setter_func
) -> None:
    """Process and store variables from a feature"""
    targets = feature.get("configuration", {}).get("targets", [])
    if not targets:
        return

    # Find primary variation (highest percentage)
    distribution = targets[0].get("distribution", [])
    if not distribution:
        return

    primary_variation_id = max(distribution, key=lambda d: d.get("percentage", 0)).get(
        "_variation"
    )
    if not primary_variation_id:
        return

    # Process variables in primary variation
    for variation in feature.get("variations", []):
        if variation.get("_id") == primary_variation_id:
            for var in variation.get("variables", []):
                var_id = var.get("_var")
                var_value = var.get("value")

                if var_id in variable_mapping:
                    var_info = variable_mapping[var_id]
                    normalized_key = normalize_key(var_info["key"])
                    processed_value = process_devcycle_value(
                        var_value, var_info["type"]
                    )
                    await flag_setter_func(normalized_key, processed_value)
            break
