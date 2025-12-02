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
from app.core.config.static import DEVCYCLE_SERVER_KEY
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
FEATURE_FLAGS_KEY = "devcycle:flags"

# Init state
_INITIALIZED = False


# ---------------------------------------------------------------------------
#  PROCESS INDIVIDUAL FEATURE VARIABLES
# ---------------------------------------------------------------------------


async def process_feature_variables(
    feature: dict,
    variable_mapping: Dict[str, Dict[str, str]],
    setter,
) -> None:
    """Extract variables from the highest-percentage variation."""

    targets = feature.get("configuration", {}).get("targets") or []
    if not targets:
        return

    distribution = targets[0].get("distribution") or []
    if not distribution:
        return

    # Highest weight variation
    primary_var_id = max(distribution, key=lambda d: d.get("percentage", 0)).get(
        "_variation"
    )
    if not primary_var_id:
        return

    # Find variation object
    variations = feature.get("variations") or []
    primary = next((v for v in variations if v.get("_id") == primary_var_id), None)
    if not primary:
        return

    for var in primary.get("variables") or []:
        var_id = var.get("_var")
        if not var_id or var_id not in variable_mapping:
            continue

        info = variable_mapping[var_id]
        key = normalize_key(info["key"])
        processed_val = process_devcycle_value(var.get("value"), info["type"])

        await setter(key, processed_val)


# ---------------------------------------------------------------------------
#  FETCH + UPDATE FLAGS
# ---------------------------------------------------------------------------


async def fetch_and_update_feature_flags() -> bool:
    """Fetch DevCycle config and update Redis (cluster safe)."""

    if not DEVCYCLE_SERVER_KEY:
        logger.warning("DEVCYCLE_SERVER_KEY missing")
        return False

    url = f"https://config-cdn.devcycle.com/config/v1/server/{DEVCYCLE_SERVER_KEY}.json"
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(
                        f"DevCycle CDN error {resp.status}: {await resp.text()}"
                    )
                    return False
                data = await resp.json()
        logger.debug("DevCycle config fetched successfully")
        variables = data.get("variables") or []
        features = data.get("features") or []

        if not variables or not features:
            logger.info("DevCycle response contains no features/variables")
            return False
        variable_map = build_variable_mapping(variables)

        new_flags: Dict[str, Any] = {}

        async def stash_flag(k, v):
            new_flags[k] = v

        for feat in features:
            if isinstance(feat, dict):
                await process_feature_variables(feat, variable_map, stash_flag)

        # Load existing from Redis
        old_flags = await _get_all_flags_from_redis()
        logger.info(f"Old flags from Redis: {old_flags}")
        logger.debug(
            f"Loaded {len(old_flags)} existing flags from Redis: {list(old_flags.keys())}"
        )
        logger.debug(
            f"Built {len(new_flags)} new flags from DevCycle: {list(new_flags.keys())}"
        )

        # Check if there are any changes
        if old_flags == new_flags:
            logger.info("No DevCycle changes detected")
            return True

        # Store all flags as a single JSON in Redis
        redis = await get_redis_service()
        client = await redis.get_client()

        try:
            await client.set(FEATURE_FLAGS_KEY, json.dumps(new_flags))
            logger.info(
                f"DevCycle Flags Updated total={len(new_flags)} flags stored in Redis"
            )
        except Exception as e:
            logger.error(
                f"FAILED to update feature flags in Redis: {type(e).__name__}: {e}"
            )
            raise

        return True

    except aiohttp.ClientError as e:
        logger.error(f"DevCycle HTTP request failed: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        logger.error(f"DevCycle update failed at unknown step: {type(e).__name__}: {e}")
        logger.exception("Full traceback:")
        return False


# ---------------------------------------------------------------------------
#  INITIALIZE
# ---------------------------------------------------------------------------


async def initialize_feature_flags() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    logger.info("Initializing DevCycle feature flags…")

    try:
        if DEVCYCLE_SERVER_KEY:
            response = await fetch_and_update_feature_flags()
            if response:
                logger.info(
                    f"DevCycle initialized successfully ({await get_flag_count()} flags)"
                )
            else:
                logger.error(
                    "DevCycle init failed → falling back to environment variables"
                )
        else:
            logger.info("DevCycle disabled (no server key)")
    except Exception as e:
        logger.error(f"DevCycle init error: {e}")

    _INITIALIZED = True


# ---------------------------------------------------------------------------
#  PUBLIC GETTERS
# ---------------------------------------------------------------------------


def is_initialized() -> bool:
    return _INITIALIZED


async def get_flag_count() -> int:
    return len(await _get_all_flags_from_redis())


async def get_all_flags() -> Dict[str, Any]:
    return await _get_all_flags_from_redis()


async def get_config(key: str, default_value: Any, return_type: type = str) -> Any:
    """Unified: Redis → Environment → Default (async version)"""

    # Always try Redis first (don't check _INITIALIZED to support cross-process reads)
    try:
        val = await _get_flag_from_redis(key)
        if val is not None:
            converted = convert_type(val, return_type)
            if converted is not None:
                logger.debug(f"get_config({key}): Retrieved from Redis -> {converted}")
                return converted
        else:
            logger.debug(f"get_config({key}): Not found in Redis, checking environment")
    except Exception as e:
        logger.warning(
            f"get_config({key}): Redis lookup failed: {e}, falling back to environment"
        )

    env_val = get_env_value(key, return_type)
    if env_val is not None:
        logger.debug(f"get_config({key}): Retrieved from environment -> {env_val}")
        return env_val

    logger.debug(f"get_config({key}): Using default value -> {default_value}")
    return default_value


# ---------------------------------------------------------------------------
#  REDIS OPERATIONS (CLUSTER SAFE)
# ---------------------------------------------------------------------------


async def _get_flag_from_redis(key: str) -> Optional[Any]:
    try:
        redis = await get_redis_service()
        client = await redis.get_client()

        raw = await client.get(FEATURE_FLAGS_KEY)
        if not raw:
            return None

        all_flags = json.loads(raw)
        return all_flags.get(key)
    except Exception as e:
        logger.error(f"Redis get error for {key}: {e}")
        return None


async def _get_all_flags_from_redis() -> Dict[str, Any]:
    """Load all flags from single Redis key."""
    try:
        redis = await get_redis_service()
        client = await redis.get_client()

        raw = await client.get(FEATURE_FLAGS_KEY)
        if not raw:
            return {}

        return json.loads(raw)

    except Exception as e:
        logger.error(f"Error loading all flags: {e}")
        return {}
