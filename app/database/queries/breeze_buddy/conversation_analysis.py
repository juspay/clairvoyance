"""SQL for topic evaluation and analytics."""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID


def create_voice_analysis_query(lead_id: str) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO topic_result (
            channel, source_id, reseller_id, merchant_id,
            template_id, started_at
        )
        SELECT
            'VOICE', lead.id, lead.reseller_id, lead.merchant_id,
            lead.template_id, COALESCE(lead.call_initiated_time, lead.created_at)
        FROM lead_call_tracker lead
        JOIN template ON template.id = lead.template_id
        LEFT JOIN evaluation_config ec ON ec.template_id = lead.template_id
        WHERE lead.id = $1
          AND lead.status = 'FINISHED'
          AND lead.reseller_id IS NOT NULL
          AND template.configurations
                -> 'enable_topic_evaluation' = 'true'::jsonb
          AND COALESCE(ec.enabled, true)
          AND UPPER(COALESCE(lead.outcome, '')) NOT IN (
              'NO_ANSWER', 'VOICEMAIL', 'VOICE_MAIL', 'NOT_ANSWERED'
          )
          AND lower(COALESCE(lead.meta_data ->> 'is_demo', 'false')) <> 'true'
          AND lower(COALESCE(lead.meta_data ->> 'playground', 'false')) <> 'true'
          AND jsonb_typeof(lead.meta_data -> 'transcription') = 'array'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements(lead.meta_data -> 'transcription') turn
              WHERE turn ->> 'role' = 'user'
                AND btrim(COALESCE(turn ->> 'content', '')) <> ''
          )
        ON CONFLICT DO NOTHING
        RETURNING id
    """
    return query, [lead_id]


def create_chat_analysis_query(session_id: str) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO topic_result (
            channel, source_id, reseller_id, merchant_id,
            template_id, started_at
        )
        SELECT
            'CHAT', session.id::text, session.reseller_id, session.merchant_id,
            session.template_id, session.created_at
        FROM chat_session session
        JOIN template ON template.id = session.template_id
        LEFT JOIN evaluation_config ec ON ec.template_id = session.template_id
        WHERE session.id = $1::uuid
          AND session.status = 'ENDED'
          AND template.configurations
                -> 'enable_topic_evaluation' = 'true'::jsonb
          AND COALESCE(ec.enabled, true)
          AND NOT COALESCE((session.metadata ? 'demo'), false)
          AND EXISTS (
              SELECT 1
              FROM chat_message
              WHERE chat_message.session_id = session.id
                AND chat_message.role = 'user'
                AND btrim(COALESCE(chat_message.content, '')) <> ''
          )
        ON CONFLICT DO NOTHING
        RETURNING id
    """
    return query, [session_id]


def get_analysis_transcript_query(
    channel: str, source_id: str
) -> Tuple[str, List[Any]]:
    if channel == "VOICE":
        return (
            """
            SELECT meta_data -> 'transcription' AS transcript
            FROM lead_call_tracker
            WHERE id = $1
            """,
            [source_id],
        )
    return (
        """
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object('idx', idx, 'role', role, 'content', content)
                ORDER BY idx
            ) FILTER (WHERE content IS NOT NULL AND btrim(content) <> ''),
            '[]'::jsonb
        ) AS transcript
        FROM chat_message
        WHERE session_id = $1::uuid
        """,
        [source_id],
    )


def complete_analysis_query(
    analysis_id: str,
    result_json: str,
) -> Tuple[str, List[Any]]:
    query = """
        WITH completed AS (
            UPDATE topic_result
            SET status = 'COMPLETED',
                result = $2::jsonb,
                topic_types = ARRAY(
                    SELECT btrim(topic ->> 'type')
                    FROM jsonb_array_elements($2::jsonb -> 'topics') AS topic
                )
            WHERE id = $1::uuid
              AND status = 'PROCESSING'
            RETURNING template_id, result
        ), discovered AS (
            SELECT min(btrim(topic ->> 'label')) AS label
            FROM completed
            CROSS JOIN LATERAL jsonb_array_elements(
                completed.result -> 'topics'
            ) AS topic
            WHERE btrim(COALESCE(topic ->> 'label', '')) <> ''
            GROUP BY lower(btrim(topic ->> 'label'))
        )
        UPDATE evaluation_config ec
        SET topics = ec.topics || ARRAY(
            SELECT label
            FROM discovered
            WHERE NOT EXISTS (
                SELECT 1
                FROM unnest(ec.topics) AS existing(label)
                WHERE lower(btrim(existing.label)) = lower(discovered.label)
            )
            ORDER BY lower(label)
        )
        FROM completed
        WHERE ec.template_id = completed.template_id
    """
    return query, [analysis_id, result_json]


def fail_analysis_query(analysis_id: str, error_message: str) -> Tuple[str, List[Any]]:
    query = """
        UPDATE topic_result
        SET status = 'FAILED',
            result = jsonb_build_object('error', $2)
        WHERE id = $1::uuid
          AND status = 'PROCESSING'
    """
    return query, [analysis_id, error_message[:2000]]


def claim_analysis_by_id_query(analysis_id: str) -> Tuple[str, List[Any]]:
    query = """
        WITH resolved_config AS (
            SELECT
                topic_result.template_id,
                COALESCE(ec.configuration, defaults.configuration) AS configuration,
                COALESCE(ec.topics, ARRAY[]::text[]) AS topics
            FROM topic_result
            CROSS JOIN evaluation_config defaults
            LEFT JOIN evaluation_config ec
              ON ec.template_id = topic_result.template_id
            WHERE topic_result.id = $1::uuid
              AND topic_result.status = 'PENDING'
              AND defaults.template_id IS NULL
        )
        UPDATE topic_result tr
        SET status = 'PROCESSING'
        FROM resolved_config
        WHERE tr.id = $1::uuid
          AND tr.status = 'PENDING'
          AND resolved_config.template_id = tr.template_id
        RETURNING
            tr.id, tr.channel, tr.source_id,
            resolved_config.configuration AS evaluation_configuration,
            resolved_config.topics AS accepted_topics
    """
    return query, [analysis_id]


def _topic_filters(
    filters: Dict[str, Any], alias: str = "ca"
) -> Tuple[List[str], List[Any]]:
    clauses = [
        f"{alias}.status = 'COMPLETED'",
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
    if filters.get("channel"):
        add(filters["channel"], f"{alias}.channel = ${{index}}")
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


def get_topic_dashboard_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    previous_start, current_start, current_end = _topic_dashboard_dates(filters)
    scope_filters = {
        key: value
        for key, value in filters.items()
        if key not in {"date_from", "date_to", "topic_type", "topic_types"}
    }
    clauses, values = _topic_filters(scope_filters)
    values.extend([previous_start, current_start, current_end])
    previous_start_index = len(values) - 2
    current_start_index = len(values) - 1
    current_end_index = len(values)
    clauses.extend(
        [
            f"ca.started_at >= ${previous_start_index}::date",
            (f"ca.started_at < " f"(${current_end_index}::date + interval '1 day')"),
        ]
    )
    where = " AND ".join(clauses)

    query = f"""
        WITH filtered AS MATERIALIZED (
            SELECT
                ca.id, ca.template_id, ca.channel, ca.started_at,
                ca.topic_types, ca.result
            FROM topic_result ca
            WHERE {where}
        ), agent_totals AS (
            SELECT
                template_id,
                CASE
                    WHEN started_at >= ${current_start_index}::date
                    THEN 'current' ELSE 'previous'
                END AS period,
                count(*) AS conversations
            FROM filtered
            GROUP BY template_id, period
        ), exploded AS MATERIALIZED (
            SELECT
                filtered.id, filtered.template_id, filtered.channel,
                filtered.started_at,
                CASE
                    WHEN filtered.started_at >= ${current_start_index}::date
                    THEN 'current' ELSE 'previous'
                END AS period,
                keyed.topic_type AS raw_topic_type,
                ((filtered.result -> 'topics') -> ((keyed.ordinal - 1)::integer))
                    ->> 'label' AS raw_label
            FROM filtered
            CROSS JOIN LATERAL unnest(filtered.topic_types)
                WITH ORDINALITY AS keyed(topic_type, ordinal)
        ), topic_counts AS (
            SELECT
                template_id, raw_topic_type,
                count(DISTINCT id) AS conversation_count,
                min(started_at) AS first_seen_at,
                min(raw_label) AS label
            FROM exploded
            WHERE period = 'current'
            GROUP BY template_id, raw_topic_type
        ), ranked AS (
            SELECT
                topic_counts.*,
                row_number() OVER (
                    PARTITION BY template_id
                    ORDER BY conversation_count DESC, first_seen_at, raw_topic_type
                ) AS topic_rank
            FROM topic_counts
        ), mapped AS (
            SELECT
                exploded.*,
                CASE WHEN ranked.topic_rank <= 10
                     THEN ranked.raw_topic_type ELSE '__other__' END AS topic_type,
                CASE WHEN ranked.topic_rank <= 10
                     THEN ranked.label ELSE 'Other' END AS label,
                COALESCE(ranked.topic_rank, 11) AS topic_rank
            FROM exploded
            LEFT JOIN ranked USING (template_id, raw_topic_type)
        ), summary_rows AS (
            SELECT
                mapped.period,
                mapped.template_id,
                template.name AS template_name,
                mapped.topic_type,
                mapped.label,
                min(mapped.topic_rank) AS rank,
                (mapped.topic_type = '__other__') AS is_other,
                array_agg(
                    DISTINCT mapped.raw_topic_type
                    ORDER BY mapped.raw_topic_type
                ) AS underlying_topic_types,
                count(DISTINCT mapped.raw_topic_type) AS underlying_topic_count,
                count(DISTINCT mapped.id) AS conversation_count,
                count(DISTINCT mapped.id)
                    FILTER (WHERE mapped.channel = 'VOICE') AS voice_count,
                count(DISTINCT mapped.id)
                    FILTER (WHERE mapped.channel = 'CHAT') AS chat_count,
                CASE WHEN agent_totals.conversations = 0 THEN 0
                     ELSE round(
                         count(DISTINCT mapped.id) * 100.0
                             / agent_totals.conversations,
                         2
                     )
                END AS conversation_share
            FROM mapped
            JOIN agent_totals USING (period, template_id)
            JOIN template ON template.id = mapped.template_id
            GROUP BY
                mapped.period, mapped.template_id, template.name,
                mapped.topic_type, mapped.label, agent_totals.conversations
        ), trend_rows AS (
            SELECT
                date_trunc('day', mapped.started_at) AS time_bucket,
                mapped.template_id,
                template.name AS template_name,
                mapped.topic_type,
                mapped.label,
                count(DISTINCT mapped.id) AS conversation_count
            FROM mapped
            JOIN template ON template.id = mapped.template_id
            WHERE mapped.period = 'current'
            GROUP BY
                time_bucket, mapped.template_id, template.name,
                mapped.topic_type, mapped.label
        )
        SELECT
            'summary'::text AS result_type,
            summary_rows.period,
            NULL::timestamptz AS time_bucket,
            summary_rows.template_id,
            summary_rows.template_name,
            summary_rows.topic_type,
            summary_rows.label,
            summary_rows.rank,
            summary_rows.is_other,
            summary_rows.underlying_topic_types,
            summary_rows.underlying_topic_count,
            summary_rows.conversation_count,
            summary_rows.voice_count,
            summary_rows.chat_count,
            summary_rows.conversation_share
        FROM summary_rows
        UNION ALL
        SELECT
            'trend'::text AS result_type,
            'current'::text AS period,
            trend_rows.time_bucket,
            trend_rows.template_id,
            trend_rows.template_name,
            trend_rows.topic_type,
            trend_rows.label,
            NULL::bigint AS rank,
            NULL::boolean AS is_other,
            NULL::text[] AS underlying_topic_types,
            NULL::bigint AS underlying_topic_count,
            trend_rows.conversation_count,
            NULL::bigint AS voice_count,
            NULL::bigint AS chat_count,
            NULL::numeric AS conversation_share
        FROM trend_rows
        ORDER BY result_type, period, template_name, rank, time_bucket, topic_type
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
        values.append([topic_type])
        topic_types_index = len(values)
        base_clauses.append(f"ca.topic_types @> ${topic_types_index}::text[]")
    elif topic_type == "__other__":
        values.append(filters.get("topic_types") or [])
        topic_types_index = len(values)
        base_clauses.append(f"ca.topic_types && ${topic_types_index}::text[]")

    base_where = " AND ".join(base_clauses)

    cursor_clause = ""
    if cursor_started_at is not None and cursor_id is not None:
        values.extend([cursor_started_at, cursor_id])
        cursor_clause = (
            f"AND (ca.started_at, ca.id) < "
            f"(${len(values) - 1}::timestamptz, ${len(values)}::uuid)"
        )

    values.append(limit + 1)
    page_limit_index = len(values)
    query = f"""
        SELECT
            ca.id, ca.channel, ca.source_id, ca.reseller_id,
            ca.merchant_id, ca.template_id, ca.started_at,
            ca.result -> 'topics' AS topics
        FROM topic_result ca
        WHERE {base_where} {cursor_clause}
        ORDER BY ca.started_at DESC, ca.id DESC
        LIMIT ${page_limit_index}
    """
    return query, values


def get_topics_for_source_query(
    source_id: str,
    channel: str,
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
) -> Tuple[str, List[Any]]:
    """Return topics for a source within the caller's tenant scope."""
    query = """
        SELECT result -> 'topics' AS topics
        FROM topic_result
        WHERE source_id = $1
          AND channel = $2
          AND ($3::text[] IS NULL OR reseller_id = ANY($3::text[]))
          AND ($4::text[] IS NULL OR merchant_id = ANY($4::text[]))
          AND status = 'COMPLETED'
    """
    return query, [source_id, channel, reseller_ids, merchant_ids]
