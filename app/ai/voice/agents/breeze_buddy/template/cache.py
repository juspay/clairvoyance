"""Redis-backed template cache (L2 only).

Templates are read-hot, write-rare. Without caching every chat ``/message``
turn pays a Postgres roundtrip + Pydantic decode + a fistful of
``[Deprecated]`` warnings (legacy voice config migration). With L2 the
entire fleet shares one decode every TTL window.

Bust strategy: explicit ``invalidate_template()`` from the template
PUT/DELETE handlers makes propagation eager. ``CACHE_TTL_SECONDS`` is just
a safety net for the case where bust fails (transient Redis error) or for
out-of-band writes (direct SQL, replication lag).

Cache hits also avoid the legacy-config migration path because we
serialize the post-migration ``TemplateModel`` — old deprecation warnings
fire only on the first decode of each template.
"""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id as _db_get_template_by_id,
)
from app.services.redis.client import get_redis_service, is_redis_configured

CACHE_TTL_SECONDS = 60
_KEY_PREFIX = "bb:template:"


def _key(template_id: str) -> str:
    return f"{_KEY_PREFIX}{template_id}"


async def get_template_by_id_cached(template_id: str) -> Optional[TemplateModel]:
    """Return the cached template or fetch + cache on miss.

    Falls through to the DB unconditionally if Redis is unconfigured or
    misbehaving — the cache is best-effort, never load-bearing.
    """
    if not is_redis_configured():
        return await _db_get_template_by_id(template_id)

    cache_key = _key(template_id)
    redis = await get_redis_service()

    try:
        cached = await redis.get(cache_key)
    except Exception as exc:
        logger.warning(f"template cache GET failed for {template_id}: {exc}")
        cached = None

    if cached is not None:
        try:
            return TemplateModel.model_validate_json(cached)
        except Exception as exc:
            logger.warning(
                f"template cache decode failed for {template_id}, refetching: {exc}"
            )

    template = await _db_get_template_by_id(template_id)
    if template is None:
        return None

    try:
        await redis.setex(
            cache_key,
            template.model_dump_json(),
            ttl_seconds=CACHE_TTL_SECONDS,
        )
        logger.debug(f"template cache populated: {template_id}")
    except Exception as exc:
        logger.warning(f"template cache SET failed for {template_id}: {exc}")

    return template


async def invalidate_template(template_id: str) -> None:
    """Drop the Redis entry. Idempotent; safe to call when key is absent."""
    if not is_redis_configured():
        return
    try:
        redis = await get_redis_service()
        await redis.delete(_key(template_id))
        logger.info(f"template cache invalidated: {template_id}")
    except Exception as exc:
        logger.warning(f"template cache invalidate failed for {template_id}: {exc}")


__all__ = ["CACHE_TTL_SECONDS", "get_template_by_id_cached", "invalidate_template"]
