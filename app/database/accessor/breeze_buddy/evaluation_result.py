import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_result import (
    save_evaluation_results_query,
)


async def save_evaluation_results(
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    results: List[Dict[str, Any]],
) -> None:
    query, values = save_evaluation_results_query(
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        json.dumps(results),
    )
    await run_parameterized_query(query, values)
