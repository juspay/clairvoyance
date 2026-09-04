from datetime import datetime
from typing import Any, List, Optional, Tuple


def lock_evaluation_result_query(
    source_id: str,
    evaluation_type: str,
    result: str,
) -> Tuple[str, List[Any]]:
    query = """
        SELECT pg_advisory_xact_lock(
            hashtextextended(
                json_build_array($1::text, $2::text, $3::text)::text,
                0
            )
        )
    """
    return query, [source_id, evaluation_type, result]


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


def get_evaluation_result_query(
    source_id: str,
    evaluation_type: str,
    result: str,
) -> Tuple[str, List[Any]]:
    query = """
        SELECT metadata
        FROM evaluation_result
        WHERE source_id = $1
          AND evaluation_type = $2::evaluation_type
          AND result = $3
        LIMIT 1
    """
    return query, [source_id, evaluation_type, result]


def upsert_evaluation_result_query(
    evaluation_config_id: str,
    evaluation_type: str,
    source_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    started_at: datetime,
    result: str,
    metadata_json: str,
    status: str = "COMPLETED",
) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO evaluation_result (
            evaluation_config_id, evaluation_type,
            source_id, reseller_id, merchant_id, template_id,
            started_at, status, result, metadata
        ) VALUES (
            $1::uuid, $2::evaluation_type,
            $3, $4, $5, $6::uuid,
            $7, $10, $8, $9::jsonb
        )
        ON CONFLICT (source_id, evaluation_type, result)
            WHERE result IS NOT NULL
        DO UPDATE SET
            reseller_id = EXCLUDED.reseller_id,
            merchant_id = EXCLUDED.merchant_id,
            status = EXCLUDED.status,
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
        status,
    ]


def set_evaluation_result_status_query(
    source_id: str,
    evaluation_type: str,
    result: str,
    status: str,
) -> Tuple[str, List[Any]]:
    query = """
        UPDATE evaluation_result
        SET status = $4
        WHERE source_id = $1
          AND evaluation_type = $2::evaluation_type
          AND result = $3
    """
    return query, [source_id, evaluation_type, result, status]
