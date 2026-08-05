"""Topic analytics SQL for evaluation_result."""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID


def _topic_filters(
    filters: Dict[str, Any], alias: str = "ca"
) -> Tuple[List[str], List[Any]]:
    clauses = [
        f"{alias}.status = 'COMPLETED'",
        f"{alias}.evaluation_type = 'TOPIC'",
    ]
    values: List[Any] = []

    def add(value: Any, sql: str) -> None:
        values.append(value)
        clauses.append(sql.format(index=len(values)))

    if filters.get("reseller_id"):
        add(filters["reseller_id"], f"{alias}.reseller_id = ${{index}}")
    if filters.get("reseller_ids"):
        add(filters["reseller_ids"], f"{alias}.reseller_id = ANY(${{index}}::text[])")
    if filters.get("merchant_id"):
        add(filters["merchant_id"], f"{alias}.merchant_id = ${{index}}")
    if filters.get("merchant_ids"):
        add(filters["merchant_ids"], f"{alias}.merchant_id = ANY(${{index}}::text[])")
    template_id = filters.get("template_id") or filters.get("template")
    if template_id:
        add(str(template_id), f"{alias}.template_id = ${{index}}::uuid")
    if filters.get("date_from"):
        add(filters["date_from"], f"{alias}.started_at >= ${{index}}::date")
    if filters.get("date_to"):
        add(
            filters["date_to"],
            f"{alias}.started_at < (${{index}}::date + interval '1 day')",
        )
    return clauses, values


def _topic_dashboard_dates(filters: Dict[str, Any]) -> Tuple[date, date, date]:
    current_start = filters["date_from"]
    current_end = filters["date_to"]
    period_days = (current_end - current_start).days + 1
    return current_start - timedelta(days=period_days), current_start, current_end


def get_topic_dashboard_rows_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    previous_start, _, current_end = _topic_dashboard_dates(filters)
    scope_filters = {
        key: value
        for key, value in filters.items()
        if key not in {"date_from", "date_to", "topic_type", "topic_types"}
    }
    clauses, values = _topic_filters(scope_filters)
    values.extend([previous_start, current_end])
    previous_start_index = len(values) - 1
    current_end_index = len(values)
    clauses.extend(
        [
            f"ca.started_at >= ${previous_start_index}::date",
            (f"ca.started_at < " f"(${current_end_index}::date + interval '1 day')"),
        ]
    )
    where = " AND ".join(clauses)

    query = f"""
        SELECT
            ca.source_id,
            ca.template_id,
            template.name AS template_name,
            ca.started_at,
            ca.result AS raw_topic_type,
            ca.metadata ->> 'label' AS raw_label
        FROM evaluation_result ca
        JOIN template ON template.id = ca.template_id
        WHERE {where}
          AND ca.result IS NOT NULL
        ORDER BY ca.started_at, ca.source_id, ca.result
    """
    return query, values


def get_topic_conversations_query(
    filters: Dict[str, Any],
    limit: int,
    cursor_started_at: Optional[datetime] = None,
    cursor_id: Optional[UUID] = None,
) -> Tuple[str, List[Any]]:
    base_clauses, values = _topic_filters(filters)
    topic_type = filters.get("topic_type")
    if topic_type and topic_type != "__other__":
        values.append(topic_type)
        topic_type_index = len(values)
        base_clauses.append(
            "EXISTS ("
            "SELECT 1 FROM evaluation_result matched "
            "WHERE matched.source_id = ca.source_id "
            "AND matched.status = 'COMPLETED' "
            "AND matched.evaluation_type = 'TOPIC' "
            f"AND matched.result = ${topic_type_index}"
            ")"
        )
    elif topic_type == "__other__":
        values.append(filters.get("topic_types") or [])
        topic_types_index = len(values)
        base_clauses.append(
            "EXISTS ("
            "SELECT 1 FROM evaluation_result matched "
            "WHERE matched.source_id = ca.source_id "
            "AND matched.status = 'COMPLETED' "
            "AND matched.evaluation_type = 'TOPIC' "
            f"AND matched.result = ANY(${topic_types_index}::text[])"
            ")"
        )
    base_where = " AND ".join(base_clauses)

    cursor_clause = ""
    if cursor_started_at is not None and cursor_id is not None:
        values.extend([cursor_started_at, cursor_id])
        cursor_clause = (
            f"AND (ca.started_at, ca.source_id::uuid) < "
            f"(${len(values) - 1}::timestamptz, ${len(values)}::uuid)"
        )

    values.append(limit + 1)
    page_limit_index = len(values)
    query = f"""
        SELECT
            ca.source_id::uuid AS id, ca.source_id, ca.reseller_id,
            ca.merchant_id, ca.template_id, ca.started_at,
            COALESCE(
                jsonb_agg(ca.metadata ORDER BY ca.result)
                    FILTER (WHERE ca.metadata IS NOT NULL),
                '[]'::jsonb
            ) AS topics
        FROM evaluation_result ca
        WHERE {base_where} {cursor_clause}
        GROUP BY
            ca.source_id, ca.reseller_id,
            ca.merchant_id, ca.template_id, ca.started_at
        ORDER BY ca.started_at DESC, ca.source_id::uuid DESC
        LIMIT ${page_limit_index}
    """
    return query, values


def get_topics_for_source_query(
    source_id: str,
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
) -> Tuple[str, List[Any]]:
    """Return topics for a source within the caller's tenant scope."""
    query = """
        SELECT COALESCE(
            jsonb_agg(metadata ORDER BY result)
                FILTER (WHERE metadata IS NOT NULL),
            '[]'::jsonb
        ) AS topics
        FROM evaluation_result
        WHERE source_id = $1
          AND ($2::text[] IS NULL OR reseller_id = ANY($2::text[]))
          AND ($3::text[] IS NULL OR merchant_id = ANY($3::text[]))
          AND status = 'COMPLETED'
          AND evaluation_type = 'TOPIC'
    """
    return query, [source_id, reseller_ids, merchant_ids]
