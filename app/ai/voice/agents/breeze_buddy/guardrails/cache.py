"""Redis-backed cache for per-template Guardrail configuration."""

import hashlib
import json

from app.core.logger import logger
from app.database.accessor.breeze_buddy.evaluation_config import (
    get_evaluation_config,
)
from app.schemas.breeze_buddy.conversation_analysis import EvaluationType
from app.services.redis.client import get_redis_service, is_redis_configured

from .types import GuardrailsConfig

CACHE_TTL_SECONDS = 60
_KEY_PREFIX = "bb:guardrails:"
_GENERATION_KEY_PREFIX = "bb:guardrails-generation:"
_CACHE_SCHEMA_VERSION = 2


def _key(template_id: str, generation: str) -> str:
    return f"{_KEY_PREFIX}{template_id}:{generation}"


def _generation_key(template_id: str) -> str:
    return f"{_GENERATION_KEY_PREFIX}{template_id}"


def _configuration_revision(configuration: object) -> str:
    canonical = json.dumps(
        configuration,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _cache_generation(redis, template_id: str) -> str:
    value = await redis.get(_generation_key(template_id))
    try:
        return str(max(0, int(value or 0)))
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid Guardrail cache generation for {template_id}, using zero"
        )
        return "0"


async def _load_from_database(template_id: str) -> GuardrailsConfig:
    row = await get_evaluation_config(template_id, EvaluationType.GUARDRAIL.value)
    stored_configuration = row.get("configuration") if row is not None else {}
    runtime_configuration = stored_configuration if row and row.get("enabled") else {}
    guardrails = GuardrailsConfig.model_validate(runtime_configuration)
    config_id = row.get("id") if row is not None else None
    guardrails.attach_evaluation_config_id(str(config_id) if config_id else None)
    guardrails.attach_configuration_revision(
        _configuration_revision(stored_configuration) if config_id else None
    )
    return guardrails


def _decode_cached(value: str) -> tuple[GuardrailsConfig, bool]:
    payload = json.loads(value)
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") in (1, _CACHE_SCHEMA_VERSION)
        and "configuration" in payload
    ):
        configuration = payload["configuration"]
        guardrails = GuardrailsConfig.model_validate(configuration)
        config_id = payload.get("evaluation_config_id")
        guardrails.attach_evaluation_config_id(str(config_id) if config_id else None)
        revision = payload.get("configuration_revision") or (
            _configuration_revision(configuration) if config_id else None
        )
        guardrails.attach_configuration_revision(
            str(revision) if config_id and revision else None
        )
        return guardrails, True
    # Compatibility with cache entries written before runtime provenance was
    # versioned. Decode the value only to validate its shape, then force one DB
    # refresh so the replacement entry carries unambiguous row provenance.
    return GuardrailsConfig.model_validate(payload), False


def _encode_cached(guardrails: GuardrailsConfig) -> str:
    return json.dumps(
        {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "evaluation_config_id": guardrails.evaluation_config_id,
            "configuration_revision": guardrails.configuration_revision,
            "configuration": guardrails.model_dump(mode="json", exclude_none=True),
        }
    )


async def get_guardrail_config_cached(template_id: str) -> GuardrailsConfig:
    """Return the current Guardrails, using Redis as a best-effort L2 cache.

    ``evaluation_config`` is the only persistent source. A missing row means
    disabled Guardrails; template JSON is deliberately never consulted.
    """
    if not is_redis_configured():
        return await _load_from_database(template_id)

    redis = await get_redis_service()
    try:
        generation = await _cache_generation(redis, template_id)
    except Exception as exc:
        logger.warning(
            f"Guardrail cache generation GET failed for {template_id}: {exc}"
        )
        return await _load_from_database(template_id)
    cache_key = _key(template_id, generation)
    try:
        cached = await redis.get(cache_key)
    except Exception as exc:
        logger.warning(f"Guardrail cache GET failed for {template_id}: {exc}")
        cached = None

    if cached is not None:
        try:
            decoded, current_schema = _decode_cached(cached)
            if current_schema:
                return decoded
        except Exception as exc:
            logger.warning(
                f"Guardrail cache decode failed for {template_id}, refetching: {exc}"
            )

    guardrails = await _load_from_database(template_id)
    # A PUT may have committed while this miss was reading the database. Move
    # to the new generation and reload once so this caller and all subsequent
    # callers cannot repopulate or consume the superseded cache generation.
    try:
        latest_generation = await _cache_generation(redis, template_id)
    except Exception as exc:
        logger.warning(
            f"Guardrail cache generation recheck failed for {template_id}: {exc}"
        )
        return guardrails
    if latest_generation != generation:
        generation = latest_generation
        cache_key = _key(template_id, generation)
        guardrails = await _load_from_database(template_id)
    try:
        populated = await redis.setex(
            cache_key,
            _encode_cached(guardrails),
            ttl_seconds=CACHE_TTL_SECONDS,
        )
        if populated:
            logger.debug(
                f"Guardrail cache populated: {template_id} generation={generation}"
            )
        else:
            logger.warning(f"Guardrail cache SET failed for {template_id}")
    except Exception as exc:
        logger.warning(f"Guardrail cache SET failed for {template_id}: {exc}")
    return guardrails


async def invalidate_guardrail_config(template_id: str) -> None:
    """Advance the cache generation after the database row changes."""
    if not is_redis_configured():
        return
    try:
        redis = await get_redis_service()
        # Remove the pre-generation cache key written by older deployments.
        await redis.delete(f"{_KEY_PREFIX}{template_id}")
        generation = await redis.incr(_generation_key(template_id))
        logger.info(
            f"Guardrail cache invalidated: {template_id} generation={generation}"
        )
    except Exception as exc:
        # The database remains authoritative and the TTL bounds staleness if
        # Redis is temporarily unavailable during invalidation.
        logger.warning(f"Guardrail cache invalidate failed for {template_id}: {exc}")


__all__ = [
    "CACHE_TTL_SECONDS",
    "get_guardrail_config_cached",
    "invalidate_guardrail_config",
]
