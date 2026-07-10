"""Redis cache helpers for normalized data-source bundles."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.services.redis import get_redis_service, is_redis_configured


def bundle_key(template_id: str, ref_name: str, ref_signature: str = "") -> str:
    suffix = f":{ref_signature}" if ref_signature else ""
    return f"datasource:bundle:{template_id}:{ref_name}{suffix}"


def build_ref_signature(ref: Any, source_fingerprint: str = "") -> str:
    payload = json.dumps(
        {
            "ref": ref.model_dump(mode="json"),
            "source_fingerprint": source_fingerprint,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_bundle(
    source_name: str, data_source_id: str, datasets: Dict[str, Any]
) -> Dict[str, Any]:
    payload = json.dumps(datasets, sort_keys=True, default=str)
    version = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return {
        "source": {
            "name": source_name,
            "data_source_id": data_source_id,
        },
        "snapshot": {
            "version": version,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        "datasets": datasets,
    }


async def get_cached_bundle(
    template_id: str, ref_name: str, ref_signature: str = ""
) -> Optional[Dict[str, Any]]:
    if not is_redis_configured():
        return None
    try:
        redis = await get_redis_service()
        cached = await redis.get(bundle_key(template_id, ref_name, ref_signature))
        return json.loads(cached) if cached else None
    except Exception as exc:
        logger.warning(f"Failed to read data-source bundle from Redis: {exc}")
        return None


async def set_cached_bundle(
    template_id: str,
    ref_name: str,
    bundle: Dict[str, Any],
    ttl_seconds: int,
    ref_signature: str = "",
) -> bool:
    if not is_redis_configured():
        logger.warning("Redis not configured; data-source bundle not cached")
        return False
    try:
        redis = await get_redis_service()
        return await redis.setex(
            bundle_key(template_id, ref_name, ref_signature),
            json.dumps(bundle, default=str),
            ttl_seconds,
        )
    except Exception as exc:
        logger.warning(f"Failed to write data-source bundle to Redis: {exc}")
        return False
