"""Transactional accessors for tenant-scoped persistent memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.customer_identity import (
    decode_customer_identity,
)
from app.database.decoder.breeze_buddy.user_memory import (
    decode_user_memory,
    decode_user_memory_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.customer_identity import upsert_alias_query
from app.database.queries.breeze_buddy.user_memory import (
    deduplicate_merged_memories_query,
    find_duplicate_memory_query,
    insert_user_memory_query,
    list_active_memories_query,
    prune_active_memories_query,
    purge_expired_memories_query,
    repoint_memory_key_query,
    search_active_memories_query,
    supersede_exact_fact_query,
    supersede_memory_query,
)
from app.schemas.breeze_buddy.memory import (
    CustomerIdentity,
    MemoryIdentity,
    MemoryOperation,
    MemoryUpdateOperation,
    UserMemory,
)


class CustomerIdentityConflict(ValueError):
    """A phone was observed with two distinct explicit customer IDs."""


@dataclass(frozen=True)
class PreparedMemoryOperation:
    operation: MemoryOperation
    embedding: Optional[List[float]]
    operation_key: str


async def list_user_memories(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    limit: int = 100,
) -> List[UserMemory]:
    query, values = list_active_memories_query(
        reseller_id, merchant_id, customer_key, limit
    )
    rows = await run_parameterized_query(query, values)
    return decode_user_memory_list(rows or [])


async def search_user_memories(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    embedding: List[float],
    limit: int = 5,
) -> List[UserMemory]:
    query, values = search_active_memories_query(
        reseller_id, merchant_id, customer_key, embedding, limit
    )
    rows = await run_parameterized_query(query, values)
    return decode_user_memory_list(rows or [])


async def insert_user_memory(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    key_type: str,
    fact: str,
    *,
    category: Optional[str] = None,
    structured: Optional[dict] = None,
    embedding: Optional[List[float]] = None,
    source_channel: Optional[str] = None,
    confidence: float = 1.0,
    operation_key: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> Optional[UserMemory]:
    """Insert one fact through the foundation's public accessor API."""
    query, values = insert_user_memory_query(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        customer_key=customer_key,
        key_type=key_type,
        fact=fact,
        category=category,
        structured=structured,
        embedding=embedding,
        source_channel=source_channel,
        confidence=confidence,
        operation_key=operation_key,
        expires_at=expires_at,
    )
    rows = await run_parameterized_query(query, values)
    return decode_user_memory(rows[0]) if rows else None


async def supersede_memory(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    memory_id: str,
) -> Optional[UserMemory]:
    """Supersede one fact through the foundation's public accessor API."""
    query, values = supersede_memory_query(
        reseller_id, merchant_id, customer_key, memory_id
    )
    rows = await run_parameterized_query(query, values)
    return decode_user_memory(rows[0]) if rows else None


async def merge_phone_key_into_customer_id(
    reseller_id: str,
    merchant_id: str,
    phone_key: str,
    customer_id: str,
) -> int:
    """Repoint provisional facts through the foundation's public accessor API."""
    query, values = repoint_memory_key_query(
        reseller_id,
        merchant_id,
        phone_key,
        customer_id,
    )
    rows = await run_parameterized_query(query, values)
    return len(rows or [])


async def apply_memory_operations(
    identity: MemoryIdentity,
    prepared: List[PreparedMemoryOperation],
    *,
    source_channel: str,
    expires_at: datetime,
    max_facts: int,
) -> None:
    """Apply one curator batch atomically and idempotently."""
    if not prepared:
        return
    try:
        async for connection in get_db_connection():
            async with connection.transaction():
                for item in prepared:
                    operation = item.operation
                    if operation.op == "DELETE":
                        query, values = supersede_exact_fact_query(
                            identity.reseller_id,
                            identity.merchant_id,
                            identity.customer_key,
                            operation.fact,
                        )
                        await connection.fetch(query, *values)
                        continue

                    if isinstance(operation, MemoryUpdateOperation):
                        query, values = supersede_exact_fact_query(
                            identity.reseller_id,
                            identity.merchant_id,
                            identity.customer_key,
                            operation.supersedes_fact,
                        )
                        await connection.fetch(query, *values)

                    duplicate_query, duplicate_values = find_duplicate_memory_query(
                        identity.reseller_id,
                        identity.merchant_id,
                        identity.customer_key,
                        operation.fact,
                        item.embedding,
                    )
                    if await connection.fetchrow(duplicate_query, *duplicate_values):
                        continue

                    insert_query, insert_values = insert_user_memory_query(
                        reseller_id=identity.reseller_id,
                        merchant_id=identity.merchant_id,
                        customer_key=identity.customer_key,
                        key_type=identity.key_type,
                        fact=operation.fact,
                        category=operation.category,
                        structured=operation.structured,
                        embedding=item.embedding,
                        source_channel=source_channel,
                        confidence=operation.confidence,
                        operation_key=item.operation_key,
                        expires_at=expires_at,
                    )
                    await connection.fetch(insert_query, *insert_values)

                prune_query, prune_values = prune_active_memories_query(
                    identity.reseller_id,
                    identity.merchant_id,
                    identity.customer_key,
                    max_facts,
                )
                await connection.fetch(prune_query, *prune_values)
            return
    except Exception:
        logger.exception(
            "[user_memory] atomic operation batch failed "
            f"(scope={identity.scope_digest[:12]})"
        )
        raise


async def merge_identity_records(
    identity: MemoryIdentity,
) -> tuple[MemoryIdentity, CustomerIdentity]:
    """Atomically bind the alias, repoint provisional facts, and deduplicate."""
    if not identity.phone or not identity.explicit_customer_id:
        raise ValueError("identity merge requires phone and explicit customer ID")

    canonical = identity.model_copy(
        update={
            "customer_key": identity.explicit_customer_id,
            "key_type": "customer_id",
        }
    )
    try:
        async for connection in get_db_connection():
            alias: Optional[CustomerIdentity] = None
            async with connection.transaction():
                alias_query, alias_values = upsert_alias_query(
                    identity.reseller_id,
                    identity.merchant_id,
                    identity.phone,
                    identity.explicit_customer_id,
                )
                alias_row = await connection.fetchrow(alias_query, *alias_values)
                alias = decode_customer_identity(alias_row)
                if alias is None:
                    raise RuntimeError("alias upsert returned no row")
                if alias.status == "ACTIVE":
                    repoint_query, repoint_values = repoint_memory_key_query(
                        identity.reseller_id,
                        identity.merchant_id,
                        f"phone:{identity.phone}",
                        identity.explicit_customer_id,
                        "customer_id",
                    )
                    await connection.fetch(repoint_query, *repoint_values)

                    dedup_query, dedup_values = deduplicate_merged_memories_query(
                        identity.reseller_id,
                        identity.merchant_id,
                        identity.explicit_customer_id,
                    )
                    await connection.fetch(dedup_query, *dedup_values)
            if alias is None:
                raise RuntimeError("alias transaction completed without a row")
            if alias.status != "ACTIVE":
                raise CustomerIdentityConflict(
                    "phone alias conflicts with another customer ID"
                )
            return canonical, alias
    except Exception:
        logger.exception(
            "[user_memory] identity merge failed "
            f"(scope={identity.scope_digest[:12]})"
        )
        raise
    raise RuntimeError("database connection unavailable")


async def purge_expired_user_memories(limit: int = 1000) -> int:
    """Hard-delete one bounded batch of expired pgvector facts."""
    query, values = purge_expired_memories_query(limit)
    rows = await run_parameterized_query(query, values)
    return len(rows or [])
