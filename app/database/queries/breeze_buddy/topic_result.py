from datetime import datetime
from typing import Any, List, Optional, Tuple


def save_topic_results_query(
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    topics_json: str,
) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO topic_result (
            source_id, reseller_id, merchant_id, template_id,
            started_at, status, topic_type, topic
        )
        SELECT
            $1, $2, $3, $4::uuid, $5, 'COMPLETED',
            btrim(topic ->> 'type'), topic
        FROM jsonb_array_elements($6::jsonb) AS item(topic)
        WHERE btrim(COALESCE(topic ->> 'type', '')) <> ''
        ON CONFLICT DO NOTHING
    """
    return query, [
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        topics_json,
    ]
