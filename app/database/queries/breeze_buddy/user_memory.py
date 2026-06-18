"""SQL query builders for user_memory table."""

from typing import Any, List, Optional, Tuple

USER_MEMORY_TABLE = "user_memory"


def insert_user_memory_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    key_type: str,
    fact: str,
    category: Optional[str],
    structured: dict,
    embedding: Optional[List[float]],
    source_channel: Optional[str],
    confidence: float = 1.0,
) -> Tuple[str, List[Any]]:
    text = f"""
        INSERT INTO "{USER_MEMORY_TABLE}"
        (reseller_id, merchant_id, customer_key, key_type, fact, category,
         structured, embedding, source_channel, confidence)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector, $9, $10)
        RETURNING *;
    """
    import json

    values: List[Any] = [
        reseller_id,
        merchant_id,
        customer_key,
        key_type,
        fact,
        category,
        json.dumps(structured),
        embedding,
        source_channel,
        confidence,
    ]
    return text, values


def list_active_memories_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
) -> Tuple[str, List[Any]]:
    text = f"""
        SELECT * FROM "{USER_MEMORY_TABLE}"
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
        ORDER BY created_at ASC;
    """
    values: List[Any] = [reseller_id, merchant_id, customer_key]
    return text, values


def supersede_memory_query(memory_id: str) -> Tuple[str, List[Any]]:
    text = f"""
        UPDATE "{USER_MEMORY_TABLE}"
        SET superseded_at = now(), updated_at = now()
        WHERE id = $1
        RETURNING *;
    """
    return text, [memory_id]


def repoint_memory_key_query(
    reseller_id: str,
    merchant_id: str,
    old_customer_key: str,
    new_customer_key: str,
    new_key_type: str,
) -> Tuple[str, List[Any]]:
    """Re-point provisional phone:* rows to the canonical customer_id."""
    text = f"""
        UPDATE "{USER_MEMORY_TABLE}"
        SET customer_key = $4, key_type = $5, updated_at = now()
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
        RETURNING *;
    """
    values: List[Any] = [
        reseller_id,
        merchant_id,
        old_customer_key,
        new_customer_key,
        new_key_type,
    ]
    return text, values
