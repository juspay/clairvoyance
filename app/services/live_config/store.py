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
    process_feature_variables,
)
from app.services.redis.client import get_redis_service

# Constants
FEATURE_FLAGS_PREFIX = "devcycle:flags:"

# Global state
_INITIALIZED = False


async def fetch_and_update_feature_flags() -> bool:
    """Fetch DevCycle configuration and update Redis store"""
    if not DEVCYCLE_SERVER_KEY:
        logger.warning("DEVCYCLE_SERVER_KEY not configured")
        return False

    try:
        # Fetch DevCycle configuration
        url = f"https://config-cdn.devcycle.com/config/v1/server/{DEVCYCLE_SERVER_KEY}.json"
        timeout = aiohttp.ClientTimeout(total=10)

        logger.debug(f"Fetching DevCycle config from: {url}")

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"Content-Type": "application/json"}
            ) as response:
                if response.status != 200:
                    logger.error(f"DevCycle CDN failed: {response.status}")
                    logger.error(f"Response: {await response.text()}")
                    return False

                try:
                    data = await response.json()
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse DevCycle response: {e}")
                    return False

        # Store old flags for comparison and clear existing
        old_flags = await _get_all_flags_from_redis()
        await _clear_all_flags_in_redis()

        # Process DevCycle data
        variables = data.get("variables", [])
        features = data.get("features", [])

        if isinstance(variables, list) and isinstance(features, list):
            variable_mapping = build_variable_mapping(variables)

            for feature in features:
                if isinstance(feature, dict) and "key" in feature:
                    await process_feature_variables(
                        feature, variable_mapping, _set_flag_in_redis
                    )

        # Log changes
        new_flags = await _get_all_flags_from_redis()
        logger.info(f"DevCycle flags updated: {len(old_flags)} -> {len(new_flags)}")

        changes = [
            f"{k}: {old_flags.get(k)} -> {v}"
            for k, v in new_flags.items()
            if old_flags.get(k) != v
        ]

        if changes:
            logger.info(f"Changes: {changes}")
        else:
            logger.info("No flag value changes")

        return True

    except aiohttp.ClientError as e:
        logger.error(f"DevCycle API request failed: {e}")
        if "old_flags" in locals():
            await _restore_flags_to_redis(old_flags)
        return False
    except Exception as e:
        logger.error(f"DevCycle flag update failed: {e}")
        if "old_flags" in locals():
            await _restore_flags_to_redis(old_flags)
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
