"""Parameterized SQL builders for tenant-scoped persistent memory."""

import json
from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.database.vector import vector_literal

USER_MEMORY_TABLE = "user_memory"

_FACT_COLUMNS = """
    id, reseller_id, merchant_id, customer_key, key_type, fact, category,
    structured, source_channel, confidence, operation_key, expires_at,
    superseded_at, created_at, updated_at
"""


def insert_user_memory_query(
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
    operation_key: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> Tuple[str, List[Any]]:
    text = f"""
        INSERT INTO "{USER_MEMORY_TABLE}"
        (reseller_id, merchant_id, customer_key, key_type, fact, category,
         structured, embedding, source_channel, confidence, operation_key,
         expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb,
                $8::halfvec(768), $9, $10, $11, $12)
        ON CONFLICT (reseller_id, merchant_id, customer_key, operation_key)
            WHERE operation_key IS NOT NULL
        DO NOTHING
        RETURNING {_FACT_COLUMNS};
    """
    values: List[Any] = [
        reseller_id,
        merchant_id,
        customer_key,
        key_type,
        fact,
        category,
        json.dumps(structured or {}),
        vector_literal(embedding) if embedding else None,
        source_channel,
        confidence,
        operation_key,
        expires_at,
    ]
    return text, values


def list_active_memories_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    limit: int = 100,
) -> Tuple[str, List[Any]]:
    text = f"""
        SELECT {_FACT_COLUMNS} FROM "{USER_MEMORY_TABLE}"
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY confidence DESC, updated_at DESC, created_at DESC
        LIMIT $4;
    """
    return text, [reseller_id, merchant_id, customer_key, max(1, limit)]


def search_active_memories_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    embedding: List[float],
    limit: int = 5,
) -> Tuple[str, List[Any]]:
    text = f"""
        SELECT {_FACT_COLUMNS} FROM "{USER_MEMORY_TABLE}"
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
          AND embedding IS NOT NULL
        ORDER BY embedding <=> $4::halfvec(768)
        LIMIT $5;
    """
    return text, [
        reseller_id,
        merchant_id,
        customer_key,
        vector_literal(embedding),
        max(1, limit),
    ]


def supersede_memory_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    memory_id: str,
) -> Tuple[str, List[Any]]:
    text = f"""
        UPDATE "{USER_MEMORY_TABLE}"
        SET superseded_at = now(), updated_at = now()
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND id = $4
          AND superseded_at IS NULL
        RETURNING {_FACT_COLUMNS};
    """
    return text, [reseller_id, merchant_id, customer_key, memory_id]


def repoint_memory_key_query(
    reseller_id: str,
    merchant_id: str,
    old_customer_key: str,
    new_customer_key: str,
    new_key_type: str = "customer_id",
) -> Tuple[str, List[Any]]:
    text = f"""
        UPDATE "{USER_MEMORY_TABLE}"
        SET customer_key = $4, key_type = $5, updated_at = now()
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
        RETURNING {_FACT_COLUMNS};
    """
    return text, [
        reseller_id,
        merchant_id,
        old_customer_key,
        new_customer_key,
        new_key_type,
    ]


def purge_expired_memories_query(limit: int = 1000) -> Tuple[str, List[Any]]:
    text = f"""
        DELETE FROM "{USER_MEMORY_TABLE}"
        WHERE id IN (
            SELECT id FROM "{USER_MEMORY_TABLE}"
            WHERE expires_at IS NOT NULL AND expires_at <= now()
            ORDER BY expires_at ASC
            LIMIT $1
        )
        RETURNING id;
    """
    return text, [max(1, limit)]
