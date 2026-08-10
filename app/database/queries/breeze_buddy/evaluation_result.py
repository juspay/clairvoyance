from datetime import datetime
from typing import Any, List, Optional, Tuple


def save_evaluation_results_query(
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    results_json: str,
) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO evaluation_result (
            evaluation_type,
            source_id, reseller_id, merchant_id, template_id,
            started_at, status, result_type, result
        )
        SELECT
            $1::evaluation_type,
            $2, $3, $4, $5::uuid, $6, 'COMPLETED',
            btrim(result ->> 'type'), result
        FROM jsonb_array_elements($7::jsonb) AS item(result)
        WHERE btrim(COALESCE(result ->> 'type', '')) <> ''
        ON CONFLICT DO NOTHING
    """
    return query, [
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        results_json,
    ]
