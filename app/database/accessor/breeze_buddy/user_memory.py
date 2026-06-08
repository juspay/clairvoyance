"""Thin accessors for tenant-scoped persistent-memory records."""

from datetime import datetime
from typing import List, Optional

from app.database.decoder.breeze_buddy.user_memory import (
    decode_user_memory,
    decode_user_memory_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.user_memory import (
    insert_user_memory_query,
    list_active_memories_query,
    purge_expired_memories_query,
    repoint_memory_key_query,
    search_active_memories_query,
    supersede_memory_query,
)
from app.schemas.breeze_buddy.memory import UserMemory


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
    query, values = repoint_memory_key_query(
        reseller_id,
        merchant_id,
        phone_key,
        customer_id,
    )
    rows = await run_parameterized_query(query, values)
    return len(rows or [])


async def purge_expired_user_memories(limit: int = 1000) -> int:
    query, values = purge_expired_memories_query(limit)
    rows = await run_parameterized_query(query, values)
    return len(rows or [])
