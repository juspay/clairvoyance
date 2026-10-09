"""Topic analytics SQL for evaluation_result."""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

MORE_THAN_ONE = "MORE_THAN_ONE"


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
    if filters.get("template_ids"):
        add(filters["template_ids"], f"{alias}.template_id = ANY(${{index}}::uuid[])")
    for key, dim_values in (filters.get("dims") or {}).items():
        values.append(key)
        dim = f"{alias}.metadata -> 'dims' -> ${len(values)}::text"
        add(
            dim_values,
            f"(CASE WHEN jsonb_typeof({dim}) = 'array' THEN CASE WHEN "
            f"jsonb_array_length({dim}) > 1 THEN '{MORE_THAN_ONE}' "
            f"ELSE {dim} ->> 0 END END) = ANY(${{index}}::text[])",
        )
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


def _topic_tree_window(
    filters: Dict[str, Any],
) -> Tuple[List[str], List[Any]]:
    previous_start, _, current_end = _topic_dashboard_dates(filters)
    scope_filters = {
        key: value
        for key, value in filters.items()
        if key not in {"date_from", "date_to", "topic_type", "topic_types"}
    }
    clauses, values = _topic_filters(scope_filters)
    values.extend([previous_start, current_end])
    clauses.extend(
        [
            f"ca.started_at >= ${len(values) - 1}::date",
            f"ca.started_at < (${len(values)}::date + interval '1 day')",
            "ca.result IS NOT NULL",
        ]
    )
    return clauses, values


def get_topic_tree_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Calls per topic id and day, from the start of the equal-length window
    before the range to the end of the range. A call has at most one row per
    topic id (the unique index on source_id, evaluation_type, result), so
    COUNT(*) is the call count without a DISTINCT sort."""
    clauses, values = _topic_tree_window(filters)
    where = " AND ".join(clauses)

    query = f"""
        SELECT
            ca.result AS topic_type,
            ca.started_at::date AS day,
            MIN(COALESCE(ca.metadata ->> 'topic', ca.result)) AS topic,
            MIN(
                COALESCE(
                    ca.metadata ->> 'topic_label',
                    ca.metadata ->> 'label',
                    ca.result
                )
            ) AS topic_label,
            MIN(COALESCE(ca.metadata ->> 'label', ca.result)) AS label,
            COUNT(*) AS calls
        FROM evaluation_result ca
        WHERE {where}
        GROUP BY ca.result, ca.started_at::date
    """
    return query, values


def get_topic_flat_calls_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Distinct calls among flat-catalog rows (ids without a dot), where one
    call can hold several topic rows. A two-level call holds one row."""
    clauses, values = _topic_tree_window(filters)
    values.append(filters["date_from"])
    start_index = len(values)
    where = " AND ".join(clauses)

    query = f"""
        SELECT
            COUNT(DISTINCT ca.source_id)
                FILTER (WHERE ca.started_at >= ${start_index}::date) AS calls,
            COUNT(DISTINCT ca.source_id)
                FILTER (WHERE ca.started_at < ${start_index}::date)
                AS previous_calls
        FROM evaluation_result ca
        WHERE {where}
          AND strpos(ca.result, '.') = 0
    """
    return query, values


def get_topic_examples_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """One example call per topic id from the last two days of the range: a
    grounded customer phrase of readable length first, then the newest."""
    recent = {
        **filters,
        "date_from": max(filters["date_from"], filters["date_to"] - timedelta(days=1)),
    }
    clauses, values = _topic_filters(recent)
    where = " AND ".join(clauses)

    query = f"""
        SELECT DISTINCT ON (ca.result)
            ca.result AS topic_type,
            ca.source_id,
            ca.template_id,
            ca.started_at,
            ca.metadata ->> 'phrase' AS phrase,
            ca.metadata ->> 'phrase_en' AS phrase_en,
            ca.metadata ->> 'screen_text' AS screen_text,
            ca.metadata ->> 'summary' AS summary,
            ca.metadata -> 'dims' AS dims
        FROM evaluation_result ca
        WHERE {where}
          AND ca.result IS NOT NULL
          AND COALESCE(ca.metadata ->> 'phrase', '') <> ''
        ORDER BY
            ca.result,
            (ca.metadata ->> 'grounded') IS DISTINCT FROM 'false' DESC,
            length(ca.metadata ->> 'phrase') BETWEEN 15 AND 140 DESC,
            ca.started_at DESC
    """
    return query, values


def get_topic_dim_counts_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Calls per breakdown key, value and topic id, from metadata.dims. A
    call with several values of one key counts once, under MORE_THAN_ONE,
    so every key's values add up to its calls. Only two-level rows carry
    dims and a two-level call holds one row, so COUNT(*) counts calls."""
    clauses, values = _topic_filters(filters)
    where = " AND ".join(clauses)

    query = f"""
        SELECT dim.key, one.value, ca.result AS topic_type, COUNT(*) AS calls
        FROM evaluation_result ca
        CROSS JOIN LATERAL jsonb_each(
            CASE WHEN jsonb_typeof(ca.metadata -> 'dims') = 'object'
                THEN ca.metadata -> 'dims' ELSE '{{}}'::jsonb END
        ) AS dim
        CROSS JOIN LATERAL (
            SELECT CASE WHEN jsonb_typeof(dim.value) = 'array' THEN
                CASE WHEN jsonb_array_length(dim.value) > 1
                    THEN '{MORE_THAN_ONE}' ELSE dim.value ->> 0 END
            END AS value
        ) AS one
        WHERE {where}
          AND ca.result IS NOT NULL
          AND one.value IS NOT NULL
        GROUP BY dim.key, one.value, ca.result
    """
    return query, values


def get_topic_configs_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """The TOPIC configuration parts the console reads (breakdowns,
    topic_details, funnel) of the agents in the caller's scope, through the
    template table so no evaluation rows are scanned."""
    clauses: List[str] = ["ec.evaluation_type = 'TOPIC'"]
    values: List[Any] = []

    def add(value: Any, sql: str) -> None:
        values.append(value)
        clauses.append(sql.format(index=len(values)))

    if filters.get("reseller_id"):
        add(filters["reseller_id"], "t.reseller_id = ${index}")
    if filters.get("reseller_ids"):
        add(filters["reseller_ids"], "t.reseller_id = ANY(${index}::text[])")
    if filters.get("merchant_id"):
        add(filters["merchant_id"], "t.merchant_id = ${index}")
    if filters.get("merchant_ids"):
        add(filters["merchant_ids"], "t.merchant_id = ANY(${index}::text[])")
    template_id = filters.get("template_id") or filters.get("template")
    if template_id:
        add(str(template_id), "t.id = ${index}::uuid")
    if filters.get("template_ids"):
        add(filters["template_ids"], "t.id = ANY(${index}::uuid[])")
    where = " AND ".join(clauses)

    query = f"""
        SELECT
            ec.template_id,
            ec.configuration -> 'breakdowns' AS breakdowns,
            ec.configuration -> 'topic_details' AS topic_details,
            ec.configuration -> 'funnel' AS funnel
        FROM evaluation_config ec
        JOIN template t ON t.id = ec.template_id
        WHERE {where}
        ORDER BY ec.template_id
    """
    return query, values


def get_topic_screens_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Calls per topic id and screen text the customer read out, and per
    ".other" id and the reason the model named for it (proposed); case and
    outer spaces ignored, in the range."""
    clauses, values = _topic_filters(filters)
    where = " AND ".join(clauses)

    query = f"""
        SELECT
            ca.result AS topic_type,
            'screen' AS kind,
            MIN(btrim(ca.metadata ->> 'screen_text')) AS text,
            COUNT(*) AS calls
        FROM evaluation_result ca
        WHERE {where}
          AND ca.result IS NOT NULL
          AND btrim(COALESCE(ca.metadata ->> 'screen_text', '')) <> ''
        GROUP BY ca.result, lower(btrim(ca.metadata ->> 'screen_text'))
        UNION ALL
        SELECT
            ca.result,
            'proposed',
            MIN(btrim(ca.metadata ->> 'proposed')),
            COUNT(*)
        FROM evaluation_result ca
        WHERE {where}
          AND ca.result LIKE '%.other'
          AND btrim(COALESCE(ca.metadata ->> 'proposed', '')) <> ''
        GROUP BY ca.result, lower(btrim(ca.metadata ->> 'proposed'))
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
