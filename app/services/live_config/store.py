"""
DevCycle Feature Flag Store with Redis

Redis-based feature flag storage:
1. One API call to DevCycle at startup
2. Store all flags in Redis
3. Fast Redis lookup for flag access, through a per-process memo of the
   parsed blob (LIVE_CONFIG_MEMO_TTL_SECONDS; see "REDIS OPERATIONS" below:
   another process's change is seen up to TTL seconds later, 0 = off)
4. Fallback: Redis -> environment -> default
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

# Get basic environment variables directly (to avoid circular imports)
from app.core.config.static import (
    DEVCYCLE_SERVER_KEY,
    ENABLE_REDIS_DYNAMIC_CONFIG,
    LIVE_CONFIG_MEMO_TTL_SECONDS,
)
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

        # Load existing from Redis. Fresh, not memoised: this decides whether
        # to write, and must compare against what Redis holds right now.
        old_flags = await _get_all_flags_from_redis(fresh=True)
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
        finally:
            # Even on error: a timed-out SET may still have landed.
            invalidate_flags_memo()

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


async def get_all_flags(*, fresh: bool = False) -> Dict[str, Any]:
    """All flags as a dict the caller owns (safe to mutate).

    ``fresh=True`` skips the in-process memo and reads Redis now; use it
    for read-modify-write, so a write is never built on a copy up to
    LIVE_CONFIG_MEMO_TTL_SECONDS old (that would drop another pod's change).
    """
    return await _get_all_flags_from_redis(fresh=fresh)


async def set_all_flags(flags: Dict[str, Any]) -> None:
    """Replace the whole flag blob in Redis, then drop this process's memo
    so the writer reads its own write at once. Errors propagate."""
    redis = await get_redis_service()
    client = await redis.get_client()
    try:
        await client.set(FEATURE_FLAGS_KEY, json.dumps(flags))
    finally:
        # Even on error: a timed-out SET may still have landed.
        invalidate_flags_memo()


async def get_config(key: str, default_value: Any, return_type: type = str) -> Any:
    """Unified: Redis → Environment → Default (async version)

    When ENABLE_REDIS_DYNAMIC_CONFIG is False, the Redis step is skipped entirely
    and config resolves from Environment → Default only.
    """

    # Try Redis first, unless Redis dynamic config is disabled
    if ENABLE_REDIS_DYNAMIC_CONFIG:
        try:
            val = await _get_flag_from_redis(key)
            if val is not None:
                converted = convert_type(val, return_type)
                if converted is not None:
                    logger.debug(
                        f"get_config({key}): Retrieved from Redis -> {converted}"
                    )
                    return converted
            else:
                logger.debug(
                    f"get_config({key}): Not found in Redis, checking environment"
                )
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
#
# The whole flag set lives in ONE Redis key, and every read used to GET and
# json.loads all of it to return one flag (the dispatcher alone does ~4 per
# claim). So each process keeps the last successfully parsed blob for
# LIVE_CONFIG_MEMO_TTL_SECONDS (static config, default 5):
#
# - at most one GET per TTL per process; callers that arrive while a refresh
#   is in flight share that one GET (single-flight), and a caller cancelled
#   while waiting does not cancel it for the others (asyncio.shield);
# - only a successful parse of a JSON object is kept. A Redis error, a
#   missing key or a non-object blob is NOT memoised: every such read goes
#   to Redis and logs exactly as before;
# - callers get their own copy of dicts/lists, as they did when each read
#   was a fresh json.loads, so no caller can change what the next one sees;
# - a write through this module (DevCycle sync, set_all_flags) drops the
#   memo, so the writing process reads its own write at once.
#
# The one behaviour change: a flag changed by ANOTHER process becomes
# visible here up to TTL seconds later instead of on the next read.
# LIVE_CONFIG_MEMO_TTL_SECONDS=0 turns the memo off (the exact old path).

# Injectable clock (tests replace it; never patch time.monotonic globally,
# the event loop runs on it).
_monotonic = time.monotonic

# What _read_flags_blob returns when the key is missing or empty.
_NO_BLOB: Any = object()

# (expires_at on _monotonic's clock, parsed blob). Never handed to callers.
_flags_memo: Optional[Tuple[float, Dict[str, Any]]] = None
# The one refresh in flight, if any; callers await it through shield().
_flags_refresh: Optional["asyncio.Task[Any]"] = None
# Bumped by every invalidation: a refresh started before a local write must
# not memoise the value it read before that write landed.
_flags_generation = 0


def invalidate_flags_memo() -> None:
    """Forget this process's copy of the flag blob (next read goes to Redis).

    Called after every write to FEATURE_FLAGS_KEY made in this process.
    A refresh already in flight still answers its current waiters but will
    not be memoised, and new callers start a new one."""
    global _flags_memo, _flags_refresh, _flags_generation
    _flags_generation += 1
    _flags_memo = None
    _flags_refresh = None


def _copy_json(value: Any) -> Any:
    """Deep copy of a json.loads result (dicts/lists rebuilt, scalars shared:
    str/int/float/bool/None are immutable). Faster than copy.deepcopy for
    this shape, and gives callers the same ownership a fresh json.loads did."""
    if isinstance(value, dict):
        return {k: _copy_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_json(v) for v in value]
    return value


async def _read_flags_blob() -> Any:
    """One GET + json.loads of the whole blob, no memo. Returns _NO_BLOB when
    the key is missing or empty; raises on Redis or JSON errors."""
    redis = await get_redis_service()
    client = await redis.get_client()

    raw = await client.get(FEATURE_FLAGS_KEY)
    if not raw:
        return _NO_BLOB
    return json.loads(raw)


async def _refresh_flags_memo(generation: int) -> Any:
    """Body of the single in-flight refresh. Memoises only a parsed JSON
    object, and only if no local write invalidated the memo meanwhile."""
    global _flags_memo, _flags_refresh
    try:
        # TTL counts from before the GET, so the copy is never older than TTL
        # relative to the moment Redis was read.
        read_at = _monotonic()
        parsed = await _read_flags_blob()
        if isinstance(parsed, dict) and generation == _flags_generation:
            _flags_memo = (read_at + LIVE_CONFIG_MEMO_TTL_SECONDS, parsed)
        return parsed
    finally:
        if _flags_refresh is asyncio.current_task():
            _flags_refresh = None


def _retrieve_refresh_outcome(task: "asyncio.Task[Any]") -> None:
    """Mark the refresh's exception as retrieved: when every waiter was
    cancelled nobody else reads it, and asyncio would log "Task exception
    was never retrieved". The waiters (if any) still get and log it."""
    if not task.cancelled():
        task.exception()


async def _load_flags_blob() -> Any:
    """The parsed blob through the memo (shared object: callers must copy
    before handing it out). Same contract as _read_flags_blob."""
    global _flags_refresh
    if LIVE_CONFIG_MEMO_TTL_SECONDS <= 0:
        return await _read_flags_blob()

    memo = _flags_memo
    if memo is not None and memo[0] > _monotonic():
        return memo[1]

    loop = asyncio.get_running_loop()
    task = _flags_refresh
    # A refresh left over from another event loop (run.py's startup loop, a
    # thread's loop) cannot be awaited here; start one on this loop.
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_refresh_flags_memo(_flags_generation))
        task.add_done_callback(_retrieve_refresh_outcome)
        _flags_refresh = task
    return await asyncio.shield(task)


async def _get_flag_from_redis(key: str) -> Optional[Any]:
    try:
        all_flags = await _load_flags_blob()
        if all_flags is _NO_BLOB:
            return None

        return _copy_json(all_flags.get(key))
    except Exception as e:
        logger.error(f"Redis get error for {key}: {e}")
        return None


async def _get_all_flags_from_redis(fresh: bool = False) -> Dict[str, Any]:
    """Load all flags from single Redis key (memoised unless ``fresh``)."""
    try:
        if fresh:
            all_flags = await _read_flags_blob()
        else:
            all_flags = await _load_flags_blob()
        if all_flags is _NO_BLOB:
            return {}

        return _copy_json(all_flags)

    except Exception as e:
        logger.error(f"Error loading all flags: {e}")
        return {}
