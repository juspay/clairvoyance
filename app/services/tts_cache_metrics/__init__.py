"""Redis day-counters for DragonTTS cache attribution.

Per-call cache stats (built from DragonTTS ``X-Cache`` headers by
``DragonTTSService``) are flushed here once at call end: four ``HINCRBY``s
into one hash per IST day. The ``bb_ttscache_rollup`` background task reads
these hashes and upserts absolute day totals into the ``tts_cache_daily``
table, so a hash only needs to outlive the rollup cadence (TTL 3 days).

Key:    ``ttscache:daily:{YYYY-MM-DD}`` (IST date)
Field:  ``{nested_provider}|{reseller_id}|{merchant_id}|{template_id}|{metric}``
        (``-`` when merchant/template is unknown)
Value:  integer running total for the day.

Everything here is stats-only and fail-open: a Redis hiccup must never affect
a live call, so ``flush_call_cache_stats`` logs and swallows all errors.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, cast
from zoneinfo import ZoneInfo

from app.core.logger import logger
from app.services.redis import get_redis_service

if TYPE_CHECKING:
    from app.ai.voice.tts.dragontts import DragonTTSCacheStats

IST = ZoneInfo("Asia/Kolkata")

DAY_KEY_PREFIX = "ttscache:daily:"
DAY_KEY_TTL_SECONDS = 3 * 24 * 3600

# Metric name -> DragonTTSCacheStats attribute.
_METRIC_ATTRS = {
    "hit_req": "hit_requests",
    "miss_req": "miss_requests",
    "hit_words": "hit_words",
    "miss_words": "miss_words",
}

# (provider, reseller_id, merchant_id, template_id) -> {metric: value}
DayCounters = Dict[Tuple[str, str, Optional[str], Optional[str]], Dict[str, int]]


def ist_date_str(now: Optional[datetime] = None) -> str:
    """Current (or given) moment as an IST calendar date string."""
    moment = now if now is not None else datetime.now(IST)
    return moment.astimezone(IST).strftime("%Y-%m-%d")


def day_key(date_str: str) -> str:
    return f"{DAY_KEY_PREFIX}{date_str}"


def _field(
    provider: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: Optional[str],
    metric: str,
) -> str:
    return "|".join(
        [provider, reseller_id, merchant_id or "-", template_id or "-", metric]
    )


async def flush_call_cache_stats(
    stats: "DragonTTSCacheStats",
    *,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: Optional[str],
) -> None:
    """Fold one call's cache stats into today's IST day hash. Fail-open."""
    if not stats.has_data:
        return
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        key = day_key(ist_date_str())
        async with client.pipeline(transaction=False) as pipe:
            for metric, attr in _METRIC_ATTRS.items():
                value = getattr(stats, attr)
                if value:
                    pipe.hincrby(
                        key,
                        _field(
                            stats.provider,
                            reseller_id,
                            merchant_id,
                            template_id,
                            metric,
                        ),
                        value,
                    )
            pipe.expire(key, DAY_KEY_TTL_SECONDS)
            await pipe.execute()
        logger.info(
            f"TTS cache stats flushed: provider={stats.provider} "
            f"hits={stats.hit_requests}/{stats.hit_words}w "
            f"misses={stats.miss_requests}/{stats.miss_words}w"
        )
    except Exception as e:
        logger.warning(f"TTS cache stats flush failed (ignored): {e}")


async def read_day_counters(date_str: str) -> DayCounters:
    """Read one day's hash into {(provider, reseller, merchant, template): counters}.

    Malformed fields are skipped with a warning. ``-`` sentinels come back as
    ``None`` so callers can store real NULLs. Raises on Redis errors — the
    rollup task decides how to handle those.
    """
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    raw: Dict[str, Any] = await client.hgetall(day_key(date_str))
    counters: DayCounters = {}
    for field_name, value in raw.items():
        parts = field_name.split("|")
        if len(parts) != 5 or parts[4] not in _METRIC_ATTRS:
            logger.warning(f"Skipping malformed ttscache field: {field_name!r}")
            continue
        provider, reseller_id, merchant_id, template_id, metric = parts
        combo = (
            provider,
            reseller_id,
            None if merchant_id == "-" else merchant_id,
            None if template_id == "-" else template_id,
        )
        try:
            counters.setdefault(combo, {})[metric] = int(value)
        except (TypeError, ValueError):
            logger.warning(f"Skipping non-integer ttscache value: {field_name!r}")
    return counters
