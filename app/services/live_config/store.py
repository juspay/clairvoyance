"""
DevCycle Feature Flag Store with Redis

Redis-based feature flag storage:
1. One API call to DevCycle at startup
2. Store all flags in Redis
3. Fast Redis lookup for flag access
4. Fallback: Redis -> environment -> default
"""

import json
from typing import Any, Dict, Optional

import aiohttp

# Get basic environment variables directly (to avoid circular imports)
from app.core.config.static import DEVCYCLE_SERVER_KEY, ENVIRONMENT
from app.core.logger import logger
from app.services.live_config.utils import (
    build_variable_mapping,
    convert_type,
    get_env_value,
    normalize_key,
    process_devcycle_value,
)
from app.services.redis.client import get_redis_service

# Constants
FEATURE_FLAGS_PREFIX = "devcycle:flags:"

# Global state
_INITIALIZED = False


async def process_feature_variables(
    feature: dict, variable_mapping: Dict[str, Dict[str, str]], flag_setter_func
) -> None:
    """Extract and store variables from the primary variation of a feature."""

    # Get target distribution → find primary variation → exit early if any missing
    targets = feature.get("configuration", {}).get("targets") or []
    if not targets:
        return

    distribution = targets[0].get("distribution") or []
    if not distribution:
        return

    # Find highest-percentage variation
    primary_variation_id = max(distribution, key=lambda d: d.get("percentage", 0)).get(
        "_variation"
    )
    if not primary_variation_id:
        return

    # Get matching variation
    variations = feature.get("variations") or []
    primary_variation = next(
        (v for v in variations if v.get("_id") == primary_variation_id), None
    )
    if not primary_variation:
        return

    # Extract all variables
    for var in primary_variation.get("variables") or []:
        var_id = var.get("_var")
        var_value = var.get("value")
        if not var_id or var_id not in variable_mapping:
            continue

        info = variable_mapping[var_id]
        normalized_key = normalize_key(info["key"])
        processed_value = process_devcycle_value(var_value, info["type"])
        await flag_setter_func(normalized_key, processed_value)


async def fetch_and_update_feature_flags() -> bool:
    """Fetch DevCycle configuration and update Redis store with diff-based updates."""
    if not DEVCYCLE_SERVER_KEY:
        logger.warning("DEVCYCLE_SERVER_KEY not configured")
        return False

    url = f"https://config-cdn.devcycle.com/config/v1/server/{DEVCYCLE_SERVER_KEY}.json"
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        logger.debug(f"Fetching DevCycle config: {url}")

        # ----- Fetch DevCycle JSON -----
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status != 200:
                    logger.error(f"DevCycle CDN error: {resp.status}")
                    logger.error(f"Response: {await resp.text()}")
                    return False
                data = await resp.json()

        # ----- Validate Base Payload -----
        if not isinstance(data, dict):
            logger.error("DevCycle response is not a dictionary")
            return False

        # ----- Extract and Validate Variables & Features -----
        variables = data.get("variables")
        if not isinstance(variables, list):
            logger.warning(
                f"Invalid 'variables' format ({type(variables)}). Expected list."
            )
            variables = []

        features = data.get("features")
        if not isinstance(features, list):
            logger.warning(
                f"Invalid 'features' format ({type(features)}). Expected list."
            )
            features = []

        # ----- EARLY EXIT — nothing to process -----
        if not variables or not features:
            logger.info(
                "No variables or features found. Skipping feature-flag processing."
            )
            return False

        # ----- Build New Flags -----
        new_flags: dict[str, Any] = {}

        variable_map = build_variable_mapping(variables)

        async def set_flag(key: str, value: Any) -> bool:
            new_flags[key] = value
            return True

        for feature in features:
            if isinstance(feature, dict) and "key" in feature:
                await process_feature_variables(feature, variable_map, set_flag)

        # ----- Load Old Flags -----
        old_flags = await _get_all_flags_from_redis()

        old_keys = set(old_flags.keys())
        new_keys = set(new_flags.keys())

        # Diff
        keys_to_update = {
            k for k in old_keys & new_keys if old_flags[k] != new_flags[k]
        }
        keys_to_add = new_keys - old_keys
        keys_to_delete = old_keys - new_keys

        total_changes = len(keys_to_update) + len(keys_to_add) + len(keys_to_delete)

        # ----- Nothing Changed -----
        if total_changes == 0:
            logger.info(f"No flag changes detected ({len(new_flags)} total flags)")
            return True

        # ----- Apply Redis Updates -----
        redis_service = await get_redis_service()
        redis_client = await redis_service.get_client()
        pipe = redis_client.pipeline(transaction=False)

        # Add/update
        for key in keys_to_update | keys_to_add:
            pipe.set(f"{FEATURE_FLAGS_PREFIX}{key}", json.dumps(new_flags[key]))

        # Delete
        for key in keys_to_delete:
            pipe.delete(f"{FEATURE_FLAGS_PREFIX}{key}")

        await pipe.execute()

        # ----- Log Summary -----
        logger.info(
            f"DevCycle flags updated: old={len(old_flags)}, new={len(new_flags)}, "
            f"updated={len(keys_to_update)}, added={len(keys_to_add)}, deleted={len(keys_to_delete)}"
        )

        return True

    except Exception as e:
        logger.exception(f"Error fetching DevCycle feature flags: {e}")
        return False

    except aiohttp.ClientError as e:
        logger.error(f"DevCycle API request failed: {e}")
        return False
    except Exception as e:
        logger.error(f"DevCycle flag update failed: {e}")
        return False


async def initialize_feature_flags() -> None:
    """Initialize feature flag store from DevCycle API"""
    global _INITIALIZED

    if _INITIALIZED:
        return

    # DevCycle can be used in any environment when server key is available
    if not DEVCYCLE_SERVER_KEY:
        logger.info(
            f"DevCycle initialization skipped for environment: {ENVIRONMENT} (no server key)"
        )
        logger.info("Using environment variables only when DevCycle is not configured")
        _INITIALIZED = True
        return

    logger.info("Initializing DevCycle feature flags...")

    try:
        if not DEVCYCLE_SERVER_KEY:
            logger.info(
                "No DEVCYCLE_SERVER_KEY found, using environment variables only"
            )
        else:
            success = await fetch_and_update_feature_flags()
            if success:
                flag_count = await get_flag_count()
                store = await get_all_flags()
                logger.info(f"DevCycle initialized with {flag_count} feature flags")
                logger.info(f"Store: {store}")
            else:
                logger.error(
                    "DevCycle initialization failed, using environment variables only"
                )

    except Exception as e:
        logger.error(f"Feature flag initialization failed: {e}")

    _INITIALIZED = True
    flag_count = await get_flag_count()
    logger.info(f"Feature flag initialization completed ({flag_count} flags)")


async def get_all_flags() -> Dict[str, Any]:
    """Get all loaded feature flags"""
    return await _get_all_flags_from_redis()


def is_initialized() -> bool:
    """Check if feature flags have been initialized"""
    return _INITIALIZED


async def get_flag_count() -> int:
    """Get number of loaded flags"""
    flags = await _get_all_flags_from_redis()
    return len(flags)


async def get_config(key: str, default_value: Any, return_type: type = str) -> Any:
    """Unified configuration getter with feature flag -> env var -> default fallback"""
    try:
        # Only attempt Redis lookup if DevCycle is initialized
        if not _INITIALIZED:
            env_result = get_env_value(key, return_type)
            return env_result if env_result is not None else default_value

        # Try Redis lookup using existing service
        flag_value = await _get_flag_from_redis(key)
        if flag_value is not None:
            converted = convert_type(flag_value, return_type)
            if converted is not None:
                return converted
    except Exception as e:
        logger.debug(f"Redis lookup failed for {key}, using env fallback: {e}")

    # Fallback to environment variables
    env_result = get_env_value(key, return_type)
    return env_result if env_result is not None else default_value


async def _get_flag_from_redis(key: str) -> Optional[Any]:
    """Get a single feature flag from Redis"""
    try:
        redis_service = await get_redis_service()
        if not redis_service:
            return None
        value_str = await redis_service.get(f"{FEATURE_FLAGS_PREFIX}{key}")
        return json.loads(value_str) if value_str else None
    except Exception as e:
        logger.error(f"Error getting flag {key} from Redis: {e}")
        return None


async def _set_flag_in_redis(key: str, value: Any) -> bool:
    """Set a feature flag in Redis"""
    try:
        redis_service = await get_redis_service()
        if not redis_service:
            return False
        return await redis_service.set(
            f"{FEATURE_FLAGS_PREFIX}{key}", json.dumps(value)
        )
    except Exception as e:
        logger.error(f"Error setting flag {key} in Redis: {e}")
        return False


async def _delete_flag_from_redis(key: str) -> bool:
    """Delete a feature flag from Redis"""
    try:
        redis_service = await get_redis_service()
        if not redis_service:
            return False

        redis_client = await redis_service.get_client()
        await redis_client.delete(f"{FEATURE_FLAGS_PREFIX}{key}")
        return True
    except Exception as e:
        logger.error(f"Error deleting flag {key} from Redis: {e}")
        return False


async def _get_all_flags_from_redis() -> Dict[str, Any]:
    """Get all feature flags from Redis"""
    try:
        redis_service = await get_redis_service()
        if not redis_service:
            return {}

        redis_client = await redis_service.get_client()

        keys = await redis_client.keys(f"{FEATURE_FLAGS_PREFIX}*")
        if not keys:
            return {}

        values = await redis_client.mget(keys)
        result = {}

        for key, value in zip(keys, values):
            if value:
                flag_key = key.replace(FEATURE_FLAGS_PREFIX, "")
                try:
                    result[flag_key] = json.loads(value)
                except json.JSONDecodeError:
                    logger.error(f"Error parsing flag value for {flag_key}")

        return result
    except Exception as e:
        logger.error(f"Error getting all flags from Redis: {e}")
        return {}


async def _clear_all_flags_in_redis() -> bool:
    """Clear all feature flags from Redis"""
    try:
        redis_service = await get_redis_service()
        if not redis_service:
            return False

        redis_client = await redis_service.get_client()

        keys = await redis_client.keys(f"{FEATURE_FLAGS_PREFIX}*")
        if keys:
            await redis_client.delete(*keys)
        return True
    except Exception as e:
        logger.error(f"Error clearing flags from Redis: {e}")
        return False


async def _restore_flags_to_redis(flags: Dict[str, Any]) -> bool:
    """Restore flags to Redis (rollback on failure)"""
    try:
        await _clear_all_flags_in_redis()
        for key, value in flags.items():
            await _set_flag_in_redis(key, value)
        return True
    except Exception as e:
        logger.error(f"Error restoring flags to Redis: {e}")
        return False
