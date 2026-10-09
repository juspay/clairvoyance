import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_result import (
    get_completed_eval_names_query,
    save_evaluation_failure_query,
    save_evaluation_results_query,
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


async def save_evaluation_failure(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    error_message: str,
) -> None:
    query, values = save_evaluation_failure_query(
        evaluation_config_id,
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        error_message,
    )
    await run_parameterized_query(query, values)


async def get_completed_eval_names(source_id: str, names: List[str]) -> Set[str]:
    query, values = get_completed_eval_names_query(source_id, names)
    rows = await run_parameterized_query(query, values)
    return {row["result"] for row in rows or []}
