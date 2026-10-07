import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_result import (
    replace_topic_result_query,
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


async def replace_topic_result(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    result: Dict[str, Any],
) -> None:
    query, values = replace_topic_result_query(
        evaluation_config_id,
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        result["type"],
        json.dumps(result),
    )
    try:
        await run_parameterized_query(query, values)
    except Exception as e:
        logger.error(f"Error replacing topic result for {source_id}: {e}")
        raise
