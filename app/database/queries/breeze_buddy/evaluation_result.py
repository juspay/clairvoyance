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


def replace_topic_result_query(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    result: str,
    metadata_json: str,
) -> Tuple[str, List[Any]]:
    """One call, one row: the two-level path deletes the call's other TOPIC
    rows (an older flat run, a FAILED row, a previous classification) and
    writes the new one in the same statement, so a re-evaluation or a
    backfill can never leave two rows for one call.

    The DELETE in the CTE is not visible to the INSERT's unique check, so a
    row that already holds the same id is not deleted: ON CONFLICT updates
    it in place."""
    query = """
        WITH gone AS (
            DELETE FROM evaluation_result
            WHERE evaluation_config_id = $1::uuid
              AND evaluation_type = $2::evaluation_type
              AND source_id = $3
              AND result IS DISTINCT FROM $8
        )
        INSERT INTO evaluation_result (
            evaluation_config_id, evaluation_type,
            source_id, reseller_id, merchant_id, template_id,
            started_at, status, result, metadata
        )
        VALUES (
            $1::uuid, $2::evaluation_type,
            $3, $4, $5, $6::uuid, $7, 'COMPLETED', $8, $9::jsonb
        )
        ON CONFLICT (source_id, evaluation_type, result) WHERE result IS NOT NULL
        DO UPDATE SET
            evaluation_config_id = EXCLUDED.evaluation_config_id,
            reseller_id = EXCLUDED.reseller_id,
            merchant_id = EXCLUDED.merchant_id,
            template_id = EXCLUDED.template_id,
            started_at = EXCLUDED.started_at,
            metadata = EXCLUDED.metadata
    """
    return query, [
        evaluation_config_id,
        evaluation_type,
        source_id,
        reseller_id,
        merchant_id,
        template_id,
        started_at,
        result,
        metadata_json,
    ]
