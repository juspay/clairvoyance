"""
DevCycle Feature Flag Store with Redis
Cluster-safe, no pipelines, supports single node + cluster.
"""

import asyncio
import json
import os
from typing import Any, Dict, Optional

import aiohttp

from app.core.config.static import ENVIRONMENT
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
DEVCYCLE_SERVER_KEY = os.getenv("DEVCYCLE_SERVER_KEY", "")

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
        logger.debug(
            f"Loaded {len(old_flags)} existing flags from Redis: {list(old_flags.keys())}"
        )
        logger.debug(
            f"Built {len(new_flags)} new flags from DevCycle: {list(new_flags.keys())}"
        )

        # Detect differences
        old_keys = set(old_flags.keys())
        new_keys = set(new_flags.keys())

        to_add = new_keys - old_keys
        to_delete = old_keys - new_keys
        to_update = {k for k in old_keys & new_keys if old_flags[k] != new_flags[k]}

        changes = len(to_add) + len(to_delete) + len(to_update)

        if changes == 0:
            logger.info("No DevCycle changes detected")
            return True

        redis = await get_redis_service()
        client = await redis.get_client()

        # ---- APPLY CHANGES (NO PIPELINE) ----

        for key in to_add | to_update:
            try:
                await client.set(
                    f"{FEATURE_FLAGS_PREFIX}{key}", json.dumps(new_flags[key])
                )
            except Exception as e:
                logger.error(f"FAILED to SET key '{key}': {type(e).__name__}: {e}")
                raise

        for key in to_delete:
            try:
                await client.delete(f"{FEATURE_FLAGS_PREFIX}{key}")
            except Exception as e:
                logger.error(f"FAILED to DELETE key '{key}': {type(e).__name__}: {e}")
                raise

        logger.info(
            f"DevCycle Flags Updated → added={len(to_add)}, "
            f"updated={len(to_update)}, deleted={len(to_delete)}, total={len(new_flags)}"
        )

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
    """Unified: Redis → Environment → Default"""

    if _INITIALIZED:
        try:
            val = await _get_flag_from_redis(key)
            if val is not None:
                converted = convert_type(val, return_type)
                if converted is not None:
                    return converted
        except Exception:
            pass

    env_val = get_env_value(key, return_type)
    return env_val if env_val is not None else default_value


# ---------------------------------------------------------------------------
#  REDIS OPERATIONS (CLUSTER SAFE)
# ---------------------------------------------------------------------------


async def _get_flag_from_redis(key: str) -> Optional[Any]:
    try:
        redis = await get_redis_service()
        client = await redis.get_client()

        raw = await client.get(f"{FEATURE_FLAGS_PREFIX}{key}")
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.error(f"Redis get error for {key}: {e}")
        return None


async def _get_all_flags_from_redis() -> Dict[str, Any]:
    """Load all flags from cluster (iterate all nodes for KEYS)."""
    try:
        redis = await get_redis_service()
        client = await redis.get_client()

        # For cluster mode, KEYS only returns keys from one node
        # We need to scan all nodes
        all_keys = set()

        # Check if this is a cluster client
        if hasattr(client, "get_nodes"):
            # Cluster mode: query each node individually using execute_command
            nodes = client.get_nodes()
            logger.debug(f"Scanning {len(nodes)} cluster nodes for keys...")

            for node in nodes:
                try:
                    # Execute KEYS command on specific node
                    node_keys = await client.execute_command(
                        "KEYS", f"{FEATURE_FLAGS_PREFIX}*", target_nodes=[node]
                    )
                    if node_keys:
                        all_keys.update(node_keys)
                        logger.debug(
                            f"Found {len(node_keys)} keys on node {node.host}:{node.port}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to scan node {node.host}:{node.port}: {e}")
                    continue
        else:
            # Single node mode
            all_keys = set(await client.keys(f"{FEATURE_FLAGS_PREFIX}*"))

        logger.debug(f"Total keys found across cluster: {len(all_keys)}")

        if not all_keys:
            return {}

        # Fetch each key individually (cluster-safe)
        flags = {}
        for key in all_keys:
            try:
                val = await client.get(key)
                if val:
                    clean = key.replace(FEATURE_FLAGS_PREFIX, "")
                    flags[clean] = json.loads(val)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON for flag {key}")
            except Exception as e:
                logger.warning(f"Failed to get key {key}: {e}")
                continue

        return flags

    except Exception as e:
        logger.error(f"Error loading all flags: {e}")
        return {}
