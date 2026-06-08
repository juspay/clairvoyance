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
    return (
        text,
        [
            reseller_id,
            merchant_id,
            customer_key,
            vector_literal(embedding),
            max(1, limit),
        ],
    )


def find_duplicate_memory_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    fact: str,
    embedding: Optional[List[float]],
    distance_threshold: float = 0.08,
) -> Tuple[str, List[Any]]:
    text = f"""
        SELECT {_FACT_COLUMNS} FROM "{USER_MEMORY_TABLE}"
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
          AND (
              lower(btrim(fact)) = lower(btrim($4))
              OR (
                  $5::halfvec(768) IS NOT NULL
                  AND embedding IS NOT NULL
                  AND (embedding <=> $5::halfvec(768)) <= $6
              )
          )
        ORDER BY confidence DESC, updated_at DESC
        LIMIT 1
        FOR UPDATE;
    """
    return text, [
        reseller_id,
        merchant_id,
        customer_key,
        fact,
        vector_literal(embedding) if embedding else None,
        distance_threshold,
    ]


def supersede_exact_fact_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    fact: str,
) -> Tuple[str, List[Any]]:
    text = f"""
        UPDATE "{USER_MEMORY_TABLE}"
        SET superseded_at = now(), updated_at = now()
        WHERE reseller_id = $1
          AND merchant_id = $2
          AND customer_key = $3
          AND superseded_at IS NULL
          AND lower(btrim(fact)) = lower(btrim($4))
        RETURNING {_FACT_COLUMNS};
    """
    return text, [reseller_id, merchant_id, customer_key, fact]


def supersede_memory_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    memory_id: str,
) -> Tuple[str, List[Any]]:
    """Supersede one record while preserving the foundation's public query API."""
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


def prune_active_memories_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    max_facts: int,
) -> Tuple[str, List[Any]]:
    text = f"""
        WITH overflow AS (
            SELECT id
            FROM "{USER_MEMORY_TABLE}"
            WHERE reseller_id = $1
              AND merchant_id = $2
              AND customer_key = $3
              AND superseded_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            ORDER BY confidence DESC, updated_at DESC, created_at DESC
            OFFSET $4
        )
        UPDATE "{USER_MEMORY_TABLE}" AS memory
        SET superseded_at = now(), updated_at = now()
        FROM overflow
        WHERE memory.id = overflow.id
        RETURNING {_FACT_COLUMNS};
    """
    return text, [reseller_id, merchant_id, customer_key, max(1, max_facts)]


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


def deduplicate_merged_memories_query(
    reseller_id: str,
    merchant_id: str,
    customer_key: str,
    distance_threshold: float = 0.08,
) -> Tuple[str, List[Any]]:
    text = f"""
        UPDATE "{USER_MEMORY_TABLE}" AS loser
        SET superseded_at = now(), updated_at = now()
        WHERE loser.reseller_id = $1
          AND loser.merchant_id = $2
          AND loser.customer_key = $3
          AND loser.superseded_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM "{USER_MEMORY_TABLE}" AS winner
              WHERE winner.reseller_id = loser.reseller_id
                AND winner.merchant_id = loser.merchant_id
                AND winner.customer_key = loser.customer_key
                AND winner.superseded_at IS NULL
                AND winner.id <> loser.id
                AND (
                    lower(btrim(winner.fact)) = lower(btrim(loser.fact))
                    OR (
                        winner.embedding IS NOT NULL
                        AND loser.embedding IS NOT NULL
                        AND (winner.embedding <=> loser.embedding) <= $4
                    )
                )
                AND (
                    winner.confidence > loser.confidence
                    OR (
                        winner.confidence = loser.confidence
                        AND winner.updated_at > loser.updated_at
                    )
                    OR (
                        winner.confidence = loser.confidence
                        AND winner.updated_at = loser.updated_at
                        AND winner.id::text > loser.id::text
                    )
                )
          )
        RETURNING {_FACT_COLUMNS};
    """
    return text, [reseller_id, merchant_id, customer_key, distance_threshold]


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
