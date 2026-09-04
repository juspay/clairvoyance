import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.database import get_db_connection
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_result import (
    get_evaluation_result_query,
    lock_evaluation_result_query,
    save_evaluation_results_query,
    set_evaluation_result_status_query,
    upsert_evaluation_result_query,
)


async def save_evaluation_results(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    results: List[Dict[str, Any]],
) -> None:
    query, values = save_evaluation_results_query(
        evaluation_config_id,
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        json.dumps(results),
    )
    await run_parameterized_query(query, values)


async def set_evaluation_result_status(
    source_id: str,
    evaluation_type: str,
    result: str,
    status: str,
) -> None:
    lock_query, lock_values = lock_evaluation_result_query(
        source_id,
        evaluation_type,
        result,
    )
    status_query, status_values = set_evaluation_result_status_query(
        source_id,
        evaluation_type,
        result,
        status,
    )
    async for conn in get_db_connection():
        async with conn.transaction():
            await conn.fetch(lock_query, *lock_values)
            await conn.fetch(status_query, *status_values)
            return
    raise RuntimeError("Failed to acquire database connection")


async def merge_evaluation_result(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    result: str,
    delta: Dict[str, Any],
    merge_metadata: Callable[
        [Optional[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]
    ],
    status: str = "COMPLETED",
) -> None:
    """Serialize one metadata merge in PostgreSQL, including first insert."""
    lock_query, lock_values = lock_evaluation_result_query(
        source_id, evaluation_type, result
    )
    get_query, get_values = get_evaluation_result_query(
        source_id, evaluation_type, result
    )

    async for conn in get_db_connection():
        async with conn.transaction():
            await conn.fetch(lock_query, *lock_values)
            rows = await conn.fetch(get_query, *get_values)
            existing: Optional[Dict[str, Any]] = None
            if rows:
                raw_metadata = rows[0].get("metadata")
                if isinstance(raw_metadata, str):
                    raw_metadata = json.loads(raw_metadata)
                if raw_metadata is not None and not isinstance(raw_metadata, dict):
                    raise ValueError("evaluation_result.metadata must be a JSON object")
                existing = dict(raw_metadata) if raw_metadata is not None else None
            metadata = merge_metadata(existing, delta)
            upsert_query, upsert_values = upsert_evaluation_result_query(
                evaluation_config_id,
                evaluation_type,
                source_id,
                reseller_id,
                merchant_id,
                template_id,
                started_at,
                result,
                json.dumps(metadata),
                status,
            )
            await conn.fetch(upsert_query, *upsert_values)
            return
    raise RuntimeError("Failed to acquire database connection")
