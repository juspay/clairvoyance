"""
Feature Flag Store - Pure Redis

All feature flags are stored in a single Redis key as a flat JSON dict:
  { "FLAG_KEY": <value>, ... }

Simple flags store a plain scalar value.
Targeting / A-B test flags store a dict with ``has_targeting: true``,
a list of audience targeting rules, distribution percentages, and per-
variation values. get_config() evaluates the rules deterministically via
SHA-256 bucketing when user context is supplied. Bucket computation is a
fast pure-CPU operation (~0.1ms); no caching layer is needed on top of Redis.

The frontend/admin API writes directly to Redis via the feature-flags
endpoints. At runtime, get_config() reads: Redis -> env var -> default.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

from app.core.config.static import ENABLE_REDIS_DYNAMIC_CONFIG
from app.core.logger import logger
from app.services.live_config.utils import convert_type, get_env_value
from app.services.redis.client import get_redis_service

# Single Redis key that holds all flags as a flat JSON object
FEATURE_FLAGS_KEY = "devcycle:flags"

# Init state
_INITIALIZED = False


# ---------------------------------------------------------------------------
#  DETERMINISTIC USER BUCKETING
# ---------------------------------------------------------------------------


def _get_user_bucket(user_id: str, feature_key: str) -> int:
    """Return a stable bucket [0, 100) for (user_id, feature_key).

    SHA-256 gives uniform distribution so the same user always lands in the
    same bucket for the same flag, and different flags distribute
    independently (no correlated assignment across features).
    """
    hash_hex = hashlib.sha256(f"{user_id}:{feature_key}".encode()).hexdigest()
    return int(hash_hex[:8], 16) % 100


# ---------------------------------------------------------------------------
#  AUDIENCE FILTER EVALUATION
# ---------------------------------------------------------------------------


def _matches_filter(
    f: dict,
    user_id: Optional[str],
    user_email: Optional[str],
    custom_data: Optional[Dict[str, Any]],
) -> bool:
    """Evaluate a single audience filter dict against the supplied user context."""
    ftype = f.get("type", "")
    sub = f.get("subType", "")
    comp = f.get("comparator", "=")
    values: List[str] = [str(v) for v in f.get("values", [])]

    if ftype == "user":
        if sub == "email":
            candidate = user_email or ""
        elif sub == "userId":
            candidate = user_id or ""
        else:
            return False
    elif ftype == "customData":
        candidate = str((custom_data or {}).get(sub, ""))
    else:
        return False

    if comp == "=":
        return candidate in values
    if comp == "!=":
        return candidate not in values
    if comp == "contain":
        return any(v in candidate for v in values)
    if comp == "!contain":
        return all(v not in candidate for v in values)
    return False


def _evaluate_targeting(
    flag_data: dict,
    key: str,
    user_id: Optional[str],
    user_email: Optional[str],
    custom_data: Optional[Dict[str, Any]],
) -> Any:
    """Resolve a targeting-based flag to the appropriate variation value.

    Walk ``targets`` in order. The first target whose audience matches the
    user determines the variation via SHA-256 bucketing. If no target
    matches (or no user context is available), the top-level ``value``
    (global default) is returned.
    """
    default = flag_data.get("value")
    targets: List[dict] = flag_data.get("targets", [])
    variation_values: Dict[str, Any] = flag_data.get("variation_values", {})

    # Without a stable user identifier we cannot bucket — return default.
    bucket_id = user_email or user_id
    if not bucket_id:
        return default

    for target in targets:
        audience = target.get("audience", {})
        filters_obj = audience.get("filters", {})
        raw_filters: List[dict] = filters_obj.get("filters", [])
        operator: str = filters_obj.get("operator", "and")

        results = [
            _matches_filter(f, user_id, user_email, custom_data) for f in raw_filters
        ]

        if operator == "or":
            matched = any(results) if results else False
        else:  # "and" (default)
            matched = all(results) if results else False

        if not matched:
            continue

        # User is in this target's audience — bucket into a variation.
        bucket = _get_user_bucket(bucket_id, key)
        cumulative = 0.0
        for entry in target.get("distribution", []):
            variation_id = entry.get("_variation") or entry.get("variation", "")
            pct = float(entry.get("percentage", 0))
            cumulative += pct
            if bucket < cumulative:
                return variation_values.get(variation_id, default)

        # Distribution didn't cover bucket (misconfigured) — fall through.
        return default

    return default


# ---------------------------------------------------------------------------
#  PUBLIC GETTERS
# ---------------------------------------------------------------------------


def is_initialized() -> bool:
    return _INITIALIZED


async def get_flag_count() -> int:
    return len(await _get_all_flags_from_redis())


async def get_all_flags() -> Dict[str, Any]:
    return await _get_all_flags_from_redis()


async def get_config(
    key: str,
    default_value: Any,
    return_type: type = str,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    custom_data: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Unified config getter: Redis -> Environment -> Default.

    For simple flags the value stored in Redis is returned directly.
    For targeting-based flags (``has_targeting: true``) the value is
    resolved by evaluating the audience rules and bucketing the user
    into the appropriate variation.  When no user context is supplied
    the targeting flag's global default value is used.

    When ENABLE_REDIS_DYNAMIC_CONFIG is False, the Redis step is skipped
    entirely and config resolves from Environment -> Default only.

    Args:
        key:           Flag key (e.g. "GEMINI_TTS_MODEL").
        default_value: Fallback if not found anywhere.
        return_type:   Expected return type (bool, str, int, float).
        user_id:       Stable user identifier for A/B bucketing.
        user_email:    User e-mail (higher-priority bucket key).
        custom_data:   Arbitrary key/value pairs for custom-data filters.

    Returns:
        Config value coerced to ``return_type``.
    """
    if ENABLE_REDIS_DYNAMIC_CONFIG:
        try:
            val = await _get_flag_from_redis(key)
            if val is not None:
                # Targeting-based flag — evaluate audience rules.
                if isinstance(val, dict) and val.get("has_targeting"):
                    resolved = _evaluate_targeting(
                        val, key, user_id, user_email, custom_data
                    )
                    converted = convert_type(resolved, return_type)
                    if converted is not None:
                        logger.debug(
                            f"get_config({key}): targeting -> {converted} "
                            f"(user={user_email or user_id or 'anon'})"
                        )
                        return converted
                    return default_value
                # Simple flag — convert and return.
                converted = convert_type(val, return_type)
                if converted is not None:
                    logger.debug(f"get_config({key}): Redis -> {converted}")
                    return converted
            else:
                logger.debug(f"get_config({key}): not in Redis, checking env")
        except Exception as e:
            logger.warning(f"get_config({key}): Redis failed: {e}, falling back to env")

    env_val = get_env_value(key, return_type)
    if env_val is not None:
        logger.debug(f"get_config({key}): env -> {env_val}")
        return env_val

    logger.debug(f"get_config({key}): default -> {default_value}")
    return default_value


# ---------------------------------------------------------------------------
#  INITIALIZE  (no-op - flags are populated by frontend/admin API)
# ---------------------------------------------------------------------------


async def initialize_feature_flags() -> None:
    """
    Mark the store as initialized.
    Flags are written directly to Redis via the feature-flags API endpoints.
    Nothing to fetch at startup.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    logger.info("Feature flag store initialized (pure Redis mode)")
    _INITIALIZED = True


# ---------------------------------------------------------------------------
#  REDIS INTERNALS
# ---------------------------------------------------------------------------


async def _get_flag_from_redis(key: str) -> Optional[Any]:
    """Return the raw value for a single flag key from Redis."""
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
    """Load all flags from the single Redis key."""
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
