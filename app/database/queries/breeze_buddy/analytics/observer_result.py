"""SQL for observer evaluation analytics."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from app.database.queries.breeze_buddy.analytics.analytics import (
    TELEPHONY_NUMBER_TABLE,
    build_analytics_where_clause,
    convert_ist_to_utc,
)


def _provider_join(filters: Dict[str, Any]) -> str:
    """``build_analytics_where_clause`` emits ``ou.provider`` for a provider
    filter but leaves the join to the caller, so every query built on it must
    add this or the SQL references an undefined alias."""
    if not filters.get("provider"):
        return ""
    return f'LEFT JOIN "{TELEPHONY_NUMBER_TABLE}" ou ON lct.telephony_number_id = ou.id'


def _observer_result_filters(
    filters: Dict[str, Any],
    *,
    value_offset: int = 0,
) -> Tuple[List[str], List[Any]]:
    clauses = [
        "er.status = 'COMPLETED'",
        "er.evaluation_type = 'OBSERVER'",
        "er.result IS NOT NULL",
    ]
    values: List[Any] = []

    if filters.get("template") or filters.get("template_id"):
        values.append(filters.get("template_id") or filters.get("template"))
        clauses.append(f"er.template_id = ${len(values) + value_offset}::uuid")

    if filters.get("reseller_id"):
        values.append(filters["reseller_id"])
        clauses.append(f"er.reseller_id = ${len(values) + value_offset}")

    if filters.get("reseller_ids"):
        values.append(filters["reseller_ids"])
        clauses.append(f"er.reseller_id = ANY(${len(values) + value_offset})")

    if filters.get("merchant_id"):
        values.append(filters["merchant_id"])
        clauses.append(f"er.merchant_id = ${len(values) + value_offset}")

    if filters.get("merchant_ids"):
        values.append(filters["merchant_ids"])
        clauses.append(f"er.merchant_id = ANY(${len(values) + value_offset})")

    if filters.get("observer_name"):
        values.append(filters["observer_name"])
        clauses.append(f"er.result = ${len(values) + value_offset}")

    if filters.get("date_from"):
        date_from = filters["date_from"]
        if isinstance(date_from, datetime):
            values.append(convert_ist_to_utc(date_from))
        else:
            values.append(
                convert_ist_to_utc(datetime.combine(date_from, datetime.min.time()))
            )
        clauses.append(f"er.started_at >= ${len(values) + value_offset}")

    if filters.get("date_to"):
        date_to = filters["date_to"]
        if isinstance(date_to, datetime):
            values.append(convert_ist_to_utc(date_to))
        else:
            next_day_start_ist = datetime.combine(
                date_to, datetime.min.time()
            ) + timedelta(days=1)
            values.append(convert_ist_to_utc(next_day_start_ist))
        clauses.append(f"er.started_at < ${len(values) + value_offset}")

    return clauses, values


def get_observer_detection_rows_query(
    filters: Dict[str, Any],
    limit: int = 10000,
) -> Tuple[str, List[Any]]:
    clauses, values = _observer_result_filters(filters)
    clauses.append("lct.execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER')")
    values.append(limit)
    limit_index = len(values)
    where = " AND ".join(clauses)
    query = f"""
        SELECT
            er.source_id,
            er.reseller_id,
            er.merchant_id,
            er.template_id::text AS template_id,
            template.name AS template_name,
            er.started_at,
            er.result AS observer_name,
            er.metadata AS detection,
            er.metadata ->> 'handler' AS handler,
            er.metadata ->> 'action_type' AS action_type,
            er.metadata ->> 'outcome' AS outcome,
            er.metadata ->> 'node' AS node,
            lct.call_id
        FROM evaluation_result er
        LEFT JOIN template ON template.id = er.template_id
        LEFT JOIN lead_call_tracker lct ON lct.id::text = er.source_id
        WHERE {where}
        ORDER BY er.started_at DESC, er.id DESC
        LIMIT ${limit_index}
    """
    return query, values


def get_legacy_observer_detection_rows_query(
    filters: Dict[str, Any],
    limit: int = 1000,
) -> Tuple[str, List[Any]]:
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=False
    )
    conditions.extend(
        [
            "lct.execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER')",
            "lct.call_initiated_time IS NOT NULL",
            "lct.outcome IS NOT NULL",
            "observer.value ->> 'name' IS NOT NULL",
            "COALESCE((observer.value ->> 'enabled')::boolean, TRUE)",
            "observer_action.value #>> '{args,outcome}' = lct.outcome",
            (
                "NOT EXISTS ("
                "SELECT 1 FROM evaluation_result er "
                "WHERE er.evaluation_type = 'OBSERVER' "
                "AND er.source_id = lct.id::text "
                "AND er.result = observer.value ->> 'name'"
                ")"
            ),
        ]
    )

    if filters.get("observer_name"):
        values.append(filters["observer_name"])
        conditions.append(f"observer.value ->> 'name' = ${len(values)}")

    values.append(limit)
    limit_index = len(values)
    where = " AND ".join(conditions) if conditions else "TRUE"
    # DISTINCT ON collapses the action lateral: an observer configured with
    # more than one action whose args.outcome matches the call would otherwise
    # yield a row per action, double-counting one fire. Keeping the first action
    # mirrors the runtime, which reports a single action per detection.
    query = f"""
        SELECT *
        FROM (
            SELECT DISTINCT ON (lct.id, observer.value ->> 'name')
            lct.id::text AS source_id,
            lct.reseller_id,
            lct.merchant_id,
            lct.template_id::text AS template_id,
            template.name AS template_name,
            lct.call_initiated_time AS started_at,
            observer.value ->> 'name' AS observer_name,
            jsonb_build_object(
                'type', observer.value ->> 'name',
                'label', observer.value ->> 'name',
                'observer_name', observer.value ->> 'name',
                'handler', observer_action.value ->> 'handler',
                'action_type', observer_action.value ->> 'type',
                'outcome', lct.outcome,
                'legacy_inferred', TRUE
            ) AS detection,
            observer_action.value ->> 'handler' AS handler,
            observer_action.value ->> 'type' AS action_type,
            lct.outcome,
            NULL::text AS node,
            lct.call_id
        FROM lead_call_tracker lct
        JOIN template ON template.id = lct.template_id
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(template.configurations -> 'observers') = 'array'
                    THEN template.configurations -> 'observers'
                ELSE '[]'::jsonb
            END
        ) AS observer(value)
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(observer.value -> 'actions') = 'array'
                    THEN observer.value -> 'actions'
                WHEN jsonb_typeof(observer.value -> 'action') = 'object'
                    THEN jsonb_build_array(observer.value -> 'action')
                ELSE '[]'::jsonb
            END
        ) AS observer_action(value)
        {_provider_join(filters)}
        WHERE {where}
        ORDER BY lct.id, observer.value ->> 'name', lct.call_initiated_time DESC
        ) legacy_fire
        ORDER BY legacy_fire.started_at DESC, legacy_fire.source_id DESC
        LIMIT ${limit_index}
    """
    return query, values


def get_observer_eligible_conversation_count_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=False
    )
    conditions.append("lct.execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER')")
    conditions.append("lct.call_initiated_time IS NOT NULL")
    where = " AND ".join(conditions) if conditions else "TRUE"
    query = f"""
        SELECT COUNT(DISTINCT lct.id) AS total
        FROM lead_call_tracker lct
        {_provider_join(filters)}
        WHERE {where}
    """
    return query, values


def get_observer_aggregate_rows_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Counts grouped in SQL, so totals are not capped by a row limit.

    The detection-row queries fetch at most ``limit`` rows for the "recent
    fires" list; deriving totals from that list under-reports every number once
    a template crosses the cap. This aggregates the full match set instead —
    one row per (observer, action, outcome, day), which stays small.
    """
    clauses, values = _observer_result_filters(filters)
    clauses.append("lct.execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER')")
    new_where = " AND ".join(clauses)

    legacy_conditions, legacy_values = build_analytics_where_clause(
        filters, filter_execution_mode=False, value_offset=len(values)
    )
    legacy_conditions.extend(
        [
            "lct.execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER')",
            "lct.call_initiated_time IS NOT NULL",
            "lct.outcome IS NOT NULL",
            "observer.value ->> 'name' IS NOT NULL",
            "COALESCE((observer.value ->> 'enabled')::boolean, TRUE)",
            "observer_action.value #>> '{args,outcome}' = lct.outcome",
            (
                "NOT EXISTS ("
                "SELECT 1 FROM evaluation_result er "
                "WHERE er.evaluation_type = 'OBSERVER' "
                "AND er.source_id = lct.id::text "
                "AND er.result = observer.value ->> 'name'"
                ")"
            ),
        ]
    )
    if filters.get("observer_name"):
        legacy_values.append(filters["observer_name"])
        legacy_conditions.append(
            f"observer.value ->> 'name' = ${len(values) + len(legacy_values)}"
        )
    legacy_where = " AND ".join(legacy_conditions) if legacy_conditions else "TRUE"
    values.extend(legacy_values)

    query = f"""
        WITH fires AS (
            SELECT
                er.source_id,
                er.result AS observer_name,
                COALESCE(
                    er.metadata ->> 'handler',
                    er.metadata ->> 'action_type',
                    'unknown'
                ) AS action,
                COALESCE(NULLIF(er.metadata ->> 'outcome', ''), 'unchanged') AS outcome,
                er.started_at
            FROM evaluation_result er
            LEFT JOIN lead_call_tracker lct ON lct.id::text = er.source_id
            WHERE {new_where}

            UNION ALL

            SELECT * FROM (
                SELECT DISTINCT ON (lct.id, observer.value ->> 'name')
                    lct.id::text AS source_id,
                    observer.value ->> 'name' AS observer_name,
                    COALESCE(
                        observer_action.value ->> 'handler',
                        observer_action.value ->> 'type',
                        'unknown'
                    ) AS action,
                    COALESCE(NULLIF(lct.outcome, ''), 'unchanged') AS outcome,
                    lct.call_initiated_time AS started_at
                FROM lead_call_tracker lct
                JOIN template ON template.id = lct.template_id
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(template.configurations -> 'observers') = 'array'
                            THEN template.configurations -> 'observers'
                        ELSE '[]'::jsonb
                    END
                ) AS observer(value)
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(observer.value -> 'actions') = 'array'
                            THEN observer.value -> 'actions'
                        WHEN jsonb_typeof(observer.value -> 'action') = 'object'
                            THEN jsonb_build_array(observer.value -> 'action')
                        ELSE '[]'::jsonb
                    END
                ) AS observer_action(value)
                {_provider_join(filters)}
                WHERE {legacy_where}
                ORDER BY lct.id, observer.value ->> 'name', lct.call_initiated_time DESC
            ) legacy_fire
        )
        SELECT
            observer_name,
            action,
            outcome,
            (started_at AT TIME ZONE 'UTC')::date AS day,
            COUNT(*) AS fires,
            MAX(started_at) AS last_triggered,
            (ARRAY_AGG(source_id ORDER BY started_at DESC))[1] AS last_source_id
        FROM fires
        GROUP BY observer_name, action, outcome, day
    """
    return query, values
