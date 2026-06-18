"""Accessor functions for user_memory table."""

from typing import List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.user_memory import (
    decode_user_memory,
    decode_user_memory_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.user_memory import (
    insert_user_memory_query,
    list_active_memories_query,
    repoint_memory_key_query,
    supersede_memory_query,
)
from app.schemas.breeze_buddy.memory import UserMemory


async def list_user_memories(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
) -> List[UserMemory]:
    try:
        q, v = list_active_memories_query(reseller_id, merchant_id, customer_key)
        rows = await run_parameterized_query(q, v)
        return decode_user_memory_list(rows or [])
    except Exception as e:
        logger.error(
            f"[user_memory] list_user_memories failed " f"(key={customer_key}): {e}",
            exc_info=True,
        )
        raise


async def insert_user_memory(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    key_type: str,
    fact: str,
    category: Optional[str] = None,
    structured: Optional[dict] = None,
    embedding: Optional[List[float]] = None,
    source_channel: Optional[str] = None,
    confidence: float = 1.0,
) -> Optional[UserMemory]:
    try:
        q, v = insert_user_memory_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            customer_key=customer_key,
            key_type=key_type,
            fact=fact,
            category=category,
            structured=structured or {},
            embedding=embedding,
            source_channel=source_channel,
            confidence=confidence,
        )
        rows = await run_parameterized_query(q, v)
        if rows:
            return decode_user_memory(rows[0])
        return None
    except Exception as e:
        logger.error(
            f"[user_memory] insert_user_memory failed "
            f"(key={customer_key}, fact={fact[:60]!r}): {e}",
            exc_info=True,
        )
        raise


async def supersede_memory(memory_id: str) -> Optional[UserMemory]:
    try:
        q, v = supersede_memory_query(memory_id)
        rows = await run_parameterized_query(q, v)
        if rows:
            return decode_user_memory(rows[0])
        return None
    except Exception as e:
        logger.error(
            f"[user_memory] supersede_memory failed (id={memory_id}): {e}",
            exc_info=True,
        )
        raise


async def merge_phone_key_into_customer_id(
    reseller_id: str,
    merchant_id: str,
    phone_key: str,
    customer_id: str,
) -> int:
    """Re-point all active phone:* rows to the canonical customer_id.

    Returns the number of rows updated.
    """
    try:
        q, v = repoint_memory_key_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            old_customer_key=phone_key,
            new_customer_key=customer_id,
            new_key_type="customer_id",
        )
        rows = await run_parameterized_query(q, v)
        count = len(rows) if rows else 0
        if count:
            logger.info(
                f"[user_memory] merged {count} rows from "
                f"{phone_key!r} -> customer_id={customer_id!r}"
            )
        return count
    except Exception as e:
        logger.error(
            f"[user_memory] merge_phone_key_into_customer_id failed "
            f"(phone_key={phone_key!r}): {e}",
            exc_info=True,
        )
        raise
