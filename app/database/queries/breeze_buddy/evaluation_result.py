from datetime import datetime
from typing import Any, List, Optional, Tuple


def save_evaluation_results_query(
    evaluation_config_id: str,
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
            evaluation_config_id, evaluation_type,
            source_id, reseller_id, merchant_id, template_id,
            started_at, status, result, metadata
        )
        SELECT
            $1::uuid, $2::evaluation_type,
            $3, $4, $5, $6::uuid, $7, 'COMPLETED',
            btrim(metadata ->> 'type'), metadata
        FROM jsonb_array_elements($8::jsonb) AS item(metadata)
        WHERE btrim(COALESCE(metadata ->> 'type', '')) <> ''
        ON CONFLICT DO NOTHING
    """
    return query, [
        evaluation_config_id,
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        results_json,
    ]


def save_evaluation_failure_query(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    error_message: str,
) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO evaluation_result (
            evaluation_config_id, evaluation_type,
            source_id, reseller_id, merchant_id, template_id,
            started_at, status, error_message
        )
        SELECT
            $1::uuid, $2::evaluation_type,
            $3::text, $4, $5, $6::uuid, $7, 'FAILED', $8
        WHERE NOT EXISTS (
            SELECT 1 FROM evaluation_result
            WHERE evaluation_config_id = $1::uuid AND source_id = $3::text
              AND status = 'FAILED'
        )
    """
    return query, [
        evaluation_config_id,
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        error_message,
    ]
