import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.topic_result import save_topic_results_query


async def save_topic_results(
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    topics: List[Dict[str, Any]],
) -> None:
    query, values = save_topic_results_query(
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        json.dumps(topics),
    )
    await run_parameterized_query(query, values)
