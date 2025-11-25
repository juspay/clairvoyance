"""
Simple DevCycle Feature Flag Store

Dead simple global dictionary approach:
1. One API call to DevCycle at startup
2. Store all flags in global dict
3. Fast dictionary lookup for flag access
4. Fallback: store -> environment -> default
"""

import os
from typing import Any, Dict

import aiohttp

from app.core.logger import logger

# Global feature flags store - simple dictionary
_FEATURE_FLAGS: Dict[str, Any] = {}
_INITIALIZED = False

# DevCycle configuration
DEVCYCLE_SERVER_KEY = os.getenv("DEVCYCLE_SERVER_KEY", "")


async def fetch_and_update_feature_flags() -> bool:
    """
    Fetch DevCycle configuration and update the global feature flags store.

    This function handles the complete flow:
    1. Fetch configuration from DevCycle API
    2. Process variables and normalize keys
    3. Update global _FEATURE_FLAGS store
    4. Log changes and results

    Returns:
        bool: True if update was successful, False otherwise
    """
    global _FEATURE_FLAGS

    try:
        if not DEVCYCLE_SERVER_KEY:
            logger.warning("DEVCYCLE_SERVER_KEY not configured")
            return False

        # Fetch DevCycle configuration
        url = f"https://config-cdn.devcycle.com/config/v1/server/{DEVCYCLE_SERVER_KEY}.json"
        headers = {"Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=10)

        logger.debug(f"Fetching DevCycle config from: {url}")

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    response_text = await response.text()
                    logger.error(f"DevCycle CDN failed: {response.status}")
                    logger.error(f"Raw Response Text: {response_text}")
                    return False

                # Parse JSON response
                import json

                try:
                    data = await response.json()
                except json.JSONDecodeError as e:
                    response_text = await response.text()
                    logger.error(f"Failed to parse DevCycle response as JSON: {e}")
                    logger.error(f"Raw response that failed to parse: {response_text}")
                    return False

        # Store old flags for comparison
        old_flags = _FEATURE_FLAGS.copy()
        old_flag_count = len(_FEATURE_FLAGS)

        # Clear and rebuild feature flags store
        _FEATURE_FLAGS.clear()

        # Process variables from DevCycle configuration
        # First, create a mapping of variable IDs to their keys and types
        variables_list = data.get("variables", [])
        variable_mapping = {}

        if isinstance(variables_list, list):
            for variable in variables_list:
                if (
                    isinstance(variable, dict)
                    and "_id" in variable
                    and "key" in variable
                ):
                    var_id = variable["_id"]
                    var_key = variable["key"]
                    var_type = variable.get("type", "String")
                    variable_mapping[var_id] = {"key": var_key, "type": var_type}

        # Process features to extract actual variable values
        features_list = data.get("features", [])

        if isinstance(features_list, list):
            for feature in features_list:
                if isinstance(feature, dict) and "key" in feature:
                    feature_key = feature["key"]

                    # Get the configuration for this feature
                    configuration = feature.get("configuration", {})
                    targets = configuration.get("targets", [])

                    if targets:
                        # Use the first target's distribution to get the primary variation
                        first_target = targets[0]
                        distribution = first_target.get("distribution", [])

                        if distribution:
                            # Find the variation with the highest percentage (primary variation)
                            primary_variation_id = None
                            max_percentage = 0

                            for dist in distribution:
                                percentage = dist.get("percentage", 0)
                                if percentage > max_percentage:
                                    max_percentage = percentage
                                    primary_variation_id = dist.get("_variation")

                            # Find the variation details
                            variations = feature.get("variations", [])
                            for variation in variations:
                                if variation.get("_id") == primary_variation_id:
                                    variables = variation.get("variables", [])

                                    # Process variables in this variation
                                    for var in variables:
                                        var_id = var.get("_var")
                                        var_value = var.get("value")

                                        if var_id in variable_mapping:
                                            var_info = variable_mapping[var_id]
                                            var_key = var_info["key"]
                                            var_type = var_info["type"]

                                            # Convert to uppercase with underscores format
                                            normalized_key = (
                                                var_key.upper()
                                                .replace("-", "_")
                                                .replace(".", "_")
                                            )

                                            # Process the value based on type
                                            processed_value = var_value

                                            # Convert string boolean values to actual booleans for Boolean type
                                            if var_type == "Boolean" and isinstance(
                                                var_value, str
                                            ):
                                                if var_value.lower() == "true":
                                                    processed_value = True
                                                elif var_value.lower() == "false":
                                                    processed_value = False
                                            elif (
                                                var_type == "String"
                                                and var_value == "boolean"
                                            ):
                                                # Handle special case where value is literally "boolean"
                                                processed_value = "boolean"
                                            elif var_type == "String" and isinstance(
                                                var_value, bool
                                            ):
                                                # Convert boolean to string for String type
                                                processed_value = str(var_value).lower()

                                            _FEATURE_FLAGS[normalized_key] = (
                                                processed_value
                                            )
                                        else:
                                            logger.warning(
                                                f"Variable ID {var_id} not found in variable mapping"
                                            )
                                    break
        else:
            logger.warning("No features found in DevCycle response")

        # Log results
        new_flag_count = len(_FEATURE_FLAGS)
        logger.info(f"DevCycle flags updated: {old_flag_count} -> {new_flag_count}")

        # Log any changes for debugging
        changes = []
        for key, new_value in _FEATURE_FLAGS.items():
            old_value = old_flags.get(key)
            if old_value != new_value:
                changes.append(f"{key}: {old_value} -> {new_value}")

        if changes:
            logger.info(f"Flag changes detected: {changes}")
        else:
            logger.info("No flag value changes detected")

        return True

    except aiohttp.ClientError as e:
        logger.error(f"DevCycle API request failed: {e}")
        # Restore old flags on failure
        _FEATURE_FLAGS.update(old_flags)
        return False
    except Exception as e:
        logger.error(f"DevCycle flag update failed: {e}")
        # Restore old flags on failure
        if "old_flags" in locals():
            _FEATURE_FLAGS.update(old_flags)
        return False


async def initialize_feature_flags() -> None:
    """
    Initialize global feature flag store from DevCycle API.

    This function should be called ONCE at application startup.
    Subsequent calls will be ignored (idempotent).
    """
    global _INITIALIZED

    if _INITIALIZED:
        return

    logger.info("Initializing DevCycle feature flags...")

    try:
        if not DEVCYCLE_SERVER_KEY:
            logger.info(
                "No DEVCYCLE_SERVER_KEY found, using environment variables only"
            )
            _INITIALIZED = True
            return

        success = await fetch_and_update_feature_flags()

        if success:
            logger.info(
                f"DevCycle successfully initialized with {len(_FEATURE_FLAGS)} feature flags"
            )
            logger.info(f"Created store: {_FEATURE_FLAGS}")
        else:
            logger.error(
                "DevCycle initialization failed, using environment variables only"
            )

    except Exception as e:
        logger.error(f"Feature flag initialization failed: {e}")

    _INITIALIZED = True
    logger.info(
        f"Feature flag initialization completed (loaded: {len(_FEATURE_FLAGS)} flags)"
    )


def get_all_flags() -> Dict[str, Any]:
    """Get all loaded feature flags (for debugging)"""
    return _FEATURE_FLAGS.copy()


def is_initialized() -> bool:
    """Check if feature flags have been initialized"""
    return _INITIALIZED


def get_flag_count() -> int:
    """Get number of loaded flags"""
    return len(_FEATURE_FLAGS)


def get_config(key: str, default_value: Any, return_type: type = str) -> Any:
    """
    Unified configuration getter with feature flag -> env var -> default fallback.

    Args:
        key: Configuration key
        default_value: Default value if not found anywhere
        return_type: Type to convert the result to (bool, str, int, float, list)

    Returns:
        Configuration value converted to the specified type.
        For list type: splits comma-separated values and strips whitespace.
    """
    # 1. Check feature flags first
    flag_value = _FEATURE_FLAGS.get(key)

    if flag_value is not None:
        # Convert feature flag value to requested type
        if return_type == bool:
            if isinstance(flag_value, bool):
                return flag_value
            return str(flag_value).lower() == "true"
        elif return_type == int:
            try:
                return int(flag_value)
            except (ValueError, TypeError):
                pass
        elif return_type == float:
            try:
                return float(flag_value)
            except (ValueError, TypeError):
                pass
        elif return_type == list:
            # Split comma-separated values and strip whitespace
            if isinstance(flag_value, list):
                return flag_value
            str_value = str(flag_value) if flag_value else ""
            return [item.strip() for item in str_value.split(",") if item.strip()]
        else:  # str or any other type
            return str(flag_value)

    # 2. Check environment variables
    env_value = os.environ.get(key)
    if env_value is not None:
        if return_type == bool:
            return env_value.lower() == "true"
        elif return_type == int:
            try:
                return int(env_value)
            except (ValueError, TypeError):
                pass
        elif return_type == float:
            try:
                return float(env_value)
            except (ValueError, TypeError):
                pass
        elif return_type == list:
            # Split comma-separated values and strip whitespace
            return [item.strip() for item in env_value.split(",") if item.strip()]
        else:  # str
            return env_value

    # 3. Return default value
    return default_value
