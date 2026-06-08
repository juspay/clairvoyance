"""Centralized, fail-closed runtime resolution for persistent memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, cast

from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.memory.identity import (
    resolve_memory_identity,
)
from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel
from app.core.config.dynamic import (
    BUDDY_MEMORY_ALLOW_PHONE_FALLBACK,
    BUDDY_MEMORY_BACKEND,
    BUDDY_MEMORY_EMBEDDING_MODEL,
    BUDDY_MEMORY_EMBEDDING_PROVIDER,
    BUDDY_MEMORY_ENABLED,
    BUDDY_MEMORY_IDENTITY_FIELD,
    BUDDY_MEMORY_PHONE_DEFAULT_REGION,
    BUDDY_MEMORY_PHONE_FIELD,
    BUDDY_MEMORY_RETENTION_DAYS,
    MEMORY_MAX_FACTS_PER_USER,
    SUPERMEMORY_API_KEY,
)
from app.core.logger import logger
from app.schemas.breeze_buddy.memory import (
    MemoryBackendName,
    MemoryEngineConfig,
    MemoryIdentity,
)
from app.schemas.embeddings import EmbeddingConfig


@dataclass(frozen=True)
class ResolvedMemoryRuntime:
    engine: MemoryEngineConfig
    identity: MemoryIdentity

    @property
    def backend(self) -> MemoryBackendName:
        return self.engine.backend

    @property
    def max_facts(self) -> int:
        return self.engine.max_facts


async def resolve_memory_engine_config() -> Optional[MemoryEngineConfig]:
    """Build one validated global engine policy from live configuration."""
    try:
        return MemoryEngineConfig(
            backend=cast(
                MemoryBackendName,
                (await BUDDY_MEMORY_BACKEND()).strip().lower(),
            ),
            identity_field=await BUDDY_MEMORY_IDENTITY_FIELD(),
            phone_field=await BUDDY_MEMORY_PHONE_FIELD(),
            phone_default_region=await BUDDY_MEMORY_PHONE_DEFAULT_REGION(),
            allow_phone_fallback=await BUDDY_MEMORY_ALLOW_PHONE_FALLBACK(),
            retention_days=await BUDDY_MEMORY_RETENTION_DAYS(),
            max_facts=await MEMORY_MAX_FACTS_PER_USER(),
            embedding=EmbeddingConfig(
                provider=cast(
                    Any,
                    (await BUDDY_MEMORY_EMBEDDING_PROVIDER()).strip().lower(),
                ),
                model=(await BUDDY_MEMORY_EMBEDDING_MODEL()).strip(),
            ),
        )
    except (ValidationError, ValueError, TypeError) as error:
        logger.error(
            "[memory.runtime] invalid global engine configuration; "
            f"memory disabled (error={type(error).__name__})"
        )
        return None


async def resolve_memory_runtime(
    configurations: Optional[ConfigurationModel],
    *,
    reseller_id: str,
    merchant_id: str,
    payload: Optional[Mapping[str, Any]],
) -> Optional[ResolvedMemoryRuntime]:
    """Apply template opt-in, global policy, tenant, and identity validation."""
    if configurations is None or configurations.memory is None:
        return None
    if not configurations.memory.enabled or not await BUDDY_MEMORY_ENABLED():
        return None
    if not reseller_id.strip() or not merchant_id.strip():
        return None

    engine = await resolve_memory_engine_config()
    if engine is None:
        return None

    identity = await resolve_memory_identity(
        reseller_id,
        merchant_id,
        dict(payload or {}),
        id_field=engine.identity_field,
        phone_field=engine.phone_field,
        phone_default_region=engine.phone_default_region,
        allow_phone_key=engine.allow_phone_fallback,
    )
    if identity is None:
        return None

    if engine.backend == "supermemory" and not await SUPERMEMORY_API_KEY():
        logger.error(
            "[memory.runtime] Supermemory selected without an API key; "
            "memory disabled"
        )
        return None

    return ResolvedMemoryRuntime(
        engine=engine,
        identity=identity,
    )
