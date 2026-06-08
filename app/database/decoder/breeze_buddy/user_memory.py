"""Decoders for user_memory and customer_identity rows."""

import json
from typing import Any, Dict, List, Optional

import asyncpg

from app.schemas.breeze_buddy.memory import UserMemory


def decode_user_memory(row: asyncpg.Record) -> Optional[UserMemory]:
    if not row:
        return None

    raw_structured = row.get("structured")
    if isinstance(raw_structured, str):
        try:
            structured: Dict[str, Any] = json.loads(raw_structured)
        except (json.JSONDecodeError, TypeError):
            structured = {}
    elif isinstance(raw_structured, dict):
        structured = raw_structured
    else:
        structured = {}

    raw_embedding = row.get("embedding")
    embedding: Optional[List[float]] = None
    if raw_embedding is not None:
        try:
            embedding = list(raw_embedding)
        except (TypeError, ValueError):
            embedding = None

    raw_confidence = row.get("confidence")
    confidence = (
        float(raw_confidence) if isinstance(raw_confidence, (int, float, str)) else 1.0
    )

    return UserMemory(
        id=row["id"],
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        customer_key=row["customer_key"],
        key_type=row["key_type"],
        fact=row["fact"],
        category=row.get("category"),
        structured=structured,
        embedding=embedding,
        source_channel=row.get("source_channel"),
        confidence=confidence,
        operation_key=row.get("operation_key"),
        expires_at=row.get("expires_at"),
        superseded_at=row.get("superseded_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_user_memory_list(rows: List[asyncpg.Record]) -> List[UserMemory]:
    if not rows:
        return []
    decoded = [decode_user_memory(r) for r in rows]
    return [m for m in decoded if m is not None]
