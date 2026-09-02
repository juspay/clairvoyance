"""Parameterized SQL builders for platform-agnostic Human Assist.

Each Human Assist ticket is its ``chat_session``. Ticket lifecycle data is kept
under ``chat_session.metadata.human_assist`` so the transcript and ticket share
one durable identifier and no second persistence table is needed.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HUMAN_ASSIST_RECORD_KEY = "human_assist"


def _visible_messages(alias: str = "m") -> str:
    """Inbox-visible message predicate; internal rows never surface."""
    return f"AND COALESCE({alias}.sender_type, '') <> 'internal'"


_VISIBLE_MESSAGE_FILTER = _visible_messages()


# Waiting tickets lead, ordered by how soon their claim deadline expires;
# everything else falls back to most-recent activity.
def _order_by(alias: str = "cs") -> str:
    return f"""
            CASE {alias}.human_assist_status
                WHEN 'PENDING' THEN 0
                WHEN 'OPEN' THEN 1
                ELSE 2
            END,
            CASE
                WHEN {alias}.human_assist_status = 'PENDING'
                THEN {alias}.metadata #>> '{{human_assist,claim_deadline_at}}'
            END ASC,
            {alias}.last_activity_at DESC
"""


def _columns(alias: str = "cs") -> str:
    record = f"{alias}.metadata->'{HUMAN_ASSIST_RECORD_KEY}'"
    return f"""
        {alias}.id AS id,
        {alias}.id AS chat_session_id,
        NULLIF({record}->>'widget_config_id', '')::uuid AS widget_config_id,
        {alias}.reseller_id,
        {alias}.merchant_id,
        {record}->>'status' AS status,
        NULLIF({record}->>'requested_at', '')::timestamptz AS requested_at,
        NULLIF({record}->>'claim_deadline_at', '')::timestamptz
            AS claim_deadline_at,
        NULLIF({record}->>'opened_at', '')::timestamptz AS opened_at,
        {record}->>'opened_by' AS opened_by,
        NULLIF({record}->>'closed_at', '')::timestamptz AS closed_at,
        {record}->>'closed_by' AS closed_by,
        {record}->>'close_reason' AS close_reason,
        {alias}.last_activity_at,
        NULLIF({record}->>'customer_last_seen_at', '')::timestamptz
            AS customer_last_seen_at,
        COALESCE({record}->'metadata', '{{}}'::jsonb) AS metadata
    """


def _utc_timestamp(expression: str) -> str:
    """Canonical sortable UTC timestamp stored in Human Assist JSONB."""
    return (
        f"to_char(({expression}) AT TIME ZONE 'UTC', "
        '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
    )


def _scope_filters(
    *,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    start_idx: int,
) -> Tuple[List[str], List[Any], int]:
    """Reseller/merchant scope WHERE clauses shared by the Inbox list, count,
    and scope-signature queries. An exact ``reseller_id``/``merchant_id``
    takes precedence over the ``ANY(...)`` list form when both are given.
    Returns ``(clauses, values, next_idx)`` so callers can keep binding
    further placeholders (e.g. ``LIMIT``/``OFFSET``) after this scope.
    """
    clauses: List[str] = []
    values: List[Any] = []
    idx = start_idx
    if reseller_id:
        clauses.append(f"cs.reseller_id = ${idx}")
        values.append(reseller_id)
        idx += 1
    elif reseller_ids is not None:
        clauses.append(f"cs.reseller_id = ANY(${idx})")
        values.append(reseller_ids)
        idx += 1
    if merchant_id:
        clauses.append(f"cs.merchant_id = ${idx}")
        values.append(merchant_id)
        idx += 1
    elif merchant_ids is not None:
        clauses.append(f"cs.merchant_id = ANY(${idx})")
        values.append(merchant_ids)
        idx += 1
    return clauses, values, idx


def create_human_assist_conversation_query(
    *,
    chat_session_id: str,
    claim_timeout_seconds: int,
    metadata: Dict[str, Any],
    notification_content: str,
    notification_blocks: List[Dict[str, Any]],
    sender_type: str,
) -> Tuple[str, List[Any]]:
    """Turn a first-handoff chat session into its own pending ticket."""
    query = f"""
        WITH created AS (
            UPDATE chat_session cs
            SET current_channel = 'HUMAN',
                handoff_happened = TRUE,
                last_activity_at = now(),
                metadata = jsonb_set(
                    cs.metadata,
                    '{{{HUMAN_ASSIST_RECORD_KEY}}}',
                    jsonb_build_object(
                        'widget_config_id', wc.id::text,
                        'status', 'PENDING',
                        'requested_at', {_utc_timestamp("now()")},
                        'claim_deadline_at',
                            {_utc_timestamp(
                                "now() + make_interval("
                                "secs => $2::double precision)"
                            )},
                        'customer_last_seen_at', {_utc_timestamp("now()")},
                        'metadata',
                            $3::jsonb || jsonb_build_object(
                                'platform', wc.human_assist_platform
                            )
                    ),
                    TRUE
                )
            FROM widget_config wc
            WHERE cs.id = $1::uuid
              AND wc.id::text = NULLIF(
                  cs.metadata #>> '{{widget,widget_config_id}}',
                  ''
              )
              AND cs.status <> 'ENDED'
              AND cs.current_channel = 'CHAT'
              AND cs.handoff_happened = FALSE
              AND cs.metadata->'{HUMAN_ASSIST_RECORD_KEY}' IS NULL
              AND wc.active = TRUE
              AND wc.human_assist_enabled = TRUE
            RETURNING cs.*
        ),
        notification AS (
            INSERT INTO chat_message (
                session_id,
                idx,
                role,
                content,
                content_blocks,
                sender_type
            )
            SELECT
                created.id,
                COALESCE(
                    (
                        SELECT MAX(message.idx) + 1
                        FROM chat_message message
                        WHERE message.session_id = created.id
                    ),
                    0
                ),
                'assistant',
                $4,
                $5::jsonb,
                $6
            FROM created
            RETURNING session_id
        )
        SELECT {_columns("created")}
        FROM created
        JOIN notification ON notification.session_id = created.id
    """
    return (
        query,
        [
            chat_session_id,
            max(1, int(claim_timeout_seconds)),
            json.dumps(metadata),
            notification_content,
            json.dumps(notification_blocks),
            sender_type,
        ],
    )


def rollover_human_assist_session_query(
    *,
    chat_session_id: str,
    claim_timeout_seconds: int,
    conversation_metadata: Dict[str, Any],
    context_content: str,
    context_blocks: List[Dict[str, Any]],
    context_sender_type: str,
    notification_content: str,
    notification_blocks: List[Dict[str, Any]],
    notification_sender_type: str,
) -> Tuple[str, List[Any]]:
    """Atomically roll a repeat handoff into a new session/ticket.

    Generic agent state is copied so cart, checkout, and client-context data
    survive. The previous session is ended only when the new session and its
    continuity message were created successfully.
    """
    query = f"""
        WITH source AS MATERIALIZED (
            SELECT
                cs.*,
                wc.id AS widget_config_id,
                wc.human_assist_platform
            FROM chat_session cs
            JOIN widget_config wc
              ON wc.id::text = NULLIF(
                  cs.metadata #>> '{{widget,widget_config_id}}',
                  ''
              )
            WHERE cs.id = $1::uuid
              AND cs.status <> 'ENDED'
              AND cs.current_channel = 'CHAT'
              AND cs.handoff_happened = TRUE
              AND COALESCE(
                  cs.metadata #>> '{{{HUMAN_ASSIST_RECORD_KEY},status}}',
                  ''
              ) NOT IN ('PENDING', 'OPEN')
              AND wc.active = TRUE
              AND wc.human_assist_enabled = TRUE
            FOR UPDATE OF cs
        ),
        created_session AS (
            INSERT INTO chat_session (
                template_id,
                reseller_id,
                merchant_id,
                metadata,
                current_channel,
                handoff_happened
            )
            SELECT
                source.template_id,
                source.reseller_id,
                source.merchant_id,
                (source.metadata - '{HUMAN_ASSIST_RECORD_KEY}')
                    || jsonb_build_object(
                        'human_assist_lineage',
                        COALESCE(
                            source.metadata->'human_assist_lineage',
                            '{{}}'::jsonb
                        ) || jsonb_build_object(
                            'previous_session_id', source.id::text,
                            'root_session_id', COALESCE(
                                source.metadata #>>
                                    '{{human_assist_lineage,root_session_id}}',
                                source.id::text
                            )
                        ),
                        '{HUMAN_ASSIST_RECORD_KEY}',
                        jsonb_build_object(
                            'widget_config_id',
                                source.widget_config_id::text,
                            'status', 'PENDING',
                            'requested_at', {_utc_timestamp("now()")},
                            'claim_deadline_at',
                                {_utc_timestamp(
                                    "now() + make_interval("
                                    "secs => $2::double precision)"
                                )},
                            'customer_last_seen_at',
                                {_utc_timestamp("now()")},
                            'metadata',
                                $3::jsonb || jsonb_build_object(
                                    'platform',
                                        source.human_assist_platform,
                                    'previous_chat_session_id',
                                        source.id::text
                                )
                        )
                    ),
                'HUMAN',
                TRUE
            FROM source
            RETURNING *
        ),
        copied_state AS (
            INSERT INTO agent_session_state (
                chat_session_id,
                data,
                updated_at
            )
            SELECT created_session.id, state.data, now()
            FROM created_session
            JOIN source ON TRUE
            JOIN agent_session_state state
              ON state.chat_session_id = source.id
            RETURNING chat_session_id
        ),
        context_message AS (
            INSERT INTO chat_message (
                session_id,
                idx,
                role,
                content,
                content_blocks,
                sender_type
            )
            SELECT
                created_session.id,
                0,
                'assistant',
                $4,
                $5::jsonb,
                $6
            FROM created_session
            RETURNING session_id
        ),
        notification AS (
            INSERT INTO chat_message (
                session_id,
                idx,
                role,
                content,
                content_blocks,
                sender_type
            )
            SELECT
                created_session.id,
                1,
                'assistant',
                $7,
                $8::jsonb,
                $9
            FROM created_session
            JOIN context_message
              ON context_message.session_id = created_session.id
            RETURNING session_id
        ),
        ended_source AS (
            UPDATE chat_session previous
            SET status = 'ENDED',
                current_channel = 'ENDED',
                ended_at = COALESCE(previous.ended_at, now()),
                ended_reason = COALESCE(
                    previous.ended_reason,
                    'human_assist_rollover'
                ),
                last_activity_at = now(),
                metadata = previous.metadata || jsonb_build_object(
                    'human_assist_successor_session_id',
                    created_session.id::text
                )
            FROM created_session
            JOIN notification
              ON notification.session_id = created_session.id
            WHERE previous.id = $1::uuid
              AND previous.status <> 'ENDED'
              AND previous.current_channel = 'CHAT'
            RETURNING previous.id
        )
        SELECT {_columns("created_session")}
        FROM created_session
        JOIN ended_source ON TRUE
    """
    return (
        query,
        [
            chat_session_id,
            max(1, int(claim_timeout_seconds)),
            json.dumps(conversation_metadata),
            context_content,
            json.dumps(context_blocks),
            context_sender_type,
            notification_content,
            json.dumps(notification_blocks),
            notification_sender_type,
        ],
    )


def merge_human_assist_metadata_query(
    conversation_id: str,
    metadata: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE chat_session cs
        SET metadata = jsonb_set(
                cs.metadata,
                '{{{HUMAN_ASSIST_RECORD_KEY},metadata}}',
                COALESCE(
                    cs.metadata
                        #> '{{{HUMAN_ASSIST_RECORD_KEY},metadata}}',
                    '{{}}'::jsonb
                ) || $2::jsonb,
                TRUE
            ),
            last_activity_at = now()
        WHERE cs.id = $1::uuid
          AND cs.metadata ? '{HUMAN_ASSIST_RECORD_KEY}'
        RETURNING {_columns("cs")}
    """
    return query, [conversation_id, json.dumps(metadata)]


def get_human_assist_conversation_query(
    conversation_id: str,
    *,
    include_stats: bool = False,
) -> Tuple[str, List[Any]]:
    stats = (
        f"""
               (SELECT COUNT(*) FROM chat_message m
                WHERE m.session_id = cs.id
                {_VISIBLE_MESSAGE_FILTER}) AS message_count,
               (SELECT m.content FROM chat_message m
                WHERE m.session_id = cs.id
                  AND m.content IS NOT NULL
                {_VISIBLE_MESSAGE_FILTER}
                ORDER BY m.idx DESC LIMIT 1) AS preview
        """
        if include_stats
        else "0::bigint AS message_count, NULL::text AS preview"
    )
    query = f"""
        SELECT {_columns("cs")},
               {stats}
        FROM chat_session cs
        WHERE cs.id = $1::uuid
          AND cs.metadata ? '{HUMAN_ASSIST_RECORD_KEY}'
    """
    return query, [conversation_id]


def get_active_human_assist_for_session_query(
    chat_session_id: str,
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_columns("cs")}
        FROM chat_session cs
        WHERE cs.id = $1::uuid
          AND cs.metadata #>> '{{{HUMAN_ASSIST_RECORD_KEY},status}}'
              IN ('PENDING', 'OPEN')
    """
    return query, [chat_session_id]


def list_human_assist_conversations_query(
    *,
    statuses: Optional[List[str]],
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    search: Optional[str],
    limit: int,
    offset: int,
) -> Tuple[str, List[Any]]:
    """One round trip: the page, its total, and every status tally.

    ``scoped`` is evaluated once and reused for the per-status tab counts
    and for the filtered page, so the Inbox never issues a query per tab.
    The message-count/preview lateral runs only over the ``limit`` rows
    that are actually returned, not over everything the scope matches.
    """
    scope_where: List[str] = [f"cs.metadata ? '{HUMAN_ASSIST_RECORD_KEY}'"]
    # $1 statuses, $2 search — always bound so the SQL shape stays stable.
    values: List[Any] = [statuses, search or None]
    scope_clauses, scope_values, idx = _scope_filters(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        start_idx=3,
    )
    scope_where.extend(scope_clauses)
    values.extend(scope_values)

    scope_where_sql = f"WHERE {' AND '.join(scope_where)}"
    query = f"""
        WITH scoped AS MATERIALIZED (
            SELECT cs.id,
                   cs.reseller_id,
                   cs.merchant_id,
                   cs.last_activity_at,
                   cs.metadata,
                   cs.metadata #>> '{{{HUMAN_ASSIST_RECORD_KEY},status}}'
                       AS human_assist_status
            FROM chat_session cs
            {scope_where_sql}
        ),
        tallies AS (
            SELECT
                COUNT(*) FILTER (
                    WHERE human_assist_status = 'PENDING'
                ) AS pending_total,
                COUNT(*) FILTER (
                    WHERE human_assist_status = 'OPEN'
                ) AS open_total,
                COUNT(*) FILTER (
                    WHERE human_assist_status = 'CLOSED'
                ) AS closed_total,
                COUNT(*) FILTER (
                    WHERE human_assist_status = 'TIMED_OUT'
                ) AS timed_out_total,
                COUNT(*) FILTER (
                    WHERE human_assist_status IN ('PENDING', 'OPEN')
                ) AS active_total
            FROM scoped
        ),
        filtered AS (
            SELECT cs.*
            FROM scoped cs
            WHERE ($1::text[] IS NULL OR cs.human_assist_status = ANY($1::text[]))
              AND (
                  $2::text IS NULL
                  OR cs.id::text ILIKE '%' || $2::text || '%'
              )
        ),
        page AS (
            SELECT cs.*
            FROM filtered cs
            ORDER BY {_order_by("cs")}
            LIMIT ${idx} OFFSET ${idx + 1}
        )
        SELECT {_columns("page")},
               stats.message_count,
               stats.preview,
               (SELECT COUNT(*) FROM filtered) AS total,
               tallies.pending_total,
               tallies.open_total,
               tallies.closed_total,
               tallies.timed_out_total,
               tallies.active_total
        FROM tallies
        LEFT JOIN page ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS message_count,
                   (
                       SELECT m2.content
                       FROM chat_message m2
                       WHERE m2.session_id = page.id
                         AND m2.content IS NOT NULL
                       {_visible_messages("m2")}
                       ORDER BY m2.idx DESC
                       LIMIT 1
                   ) AS preview
            FROM chat_message m
            WHERE m.session_id = page.id
            {_VISIBLE_MESSAGE_FILTER}
        ) stats ON TRUE
        ORDER BY {_order_by("page")}
    """
    values.extend([limit, offset])
    return query, values


def list_human_assist_transcript_query(
    session_id: str,
    after_idx: Optional[int] = None,
    limit: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """Inbox-visible transcript, optionally only the tail after ``after_idx``.

    Unlike the widget delivery log this keeps the rollover continuity row —
    the Inbox renders it as the "previous chat summary" card. ``limit`` caps
    the number of rows returned (still ordered ascending by idx); ``None``
    preserves the existing unbounded read.
    """
    query = """
        SELECT session_id, idx, role, content, created_at,
               content_blocks, ui_blocks, sender_type
        FROM chat_message m
        WHERE m.session_id = $1::uuid
          AND ($2::int IS NULL OR m.idx > $2::int)
          AND COALESCE(m.sender_type, '') <> 'internal'
        ORDER BY m.idx ASC
        LIMIT $3::int
    """
    return query, [session_id, after_idx, limit]


def get_human_assist_scope_signature_query(
    *,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
) -> Tuple[str, List[Any]]:
    """Cheap revision for repairing a missed Inbox pub/sub wake-up."""
    where: List[str] = [f"cs.metadata ? '{HUMAN_ASSIST_RECORD_KEY}'"]
    scope_clauses, values, _idx = _scope_filters(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        start_idx=1,
    )
    where.extend(scope_clauses)
    query = f"""
        SELECT COUNT(*)::bigint AS ticket_count,
               MAX(cs.last_activity_at) AS latest_activity_at
        FROM chat_session cs
        WHERE {' AND '.join(where)}
    """
    return query, values


def claim_human_assist_conversation_query(
    conversation_id: str,
    opened_by: str,
    notification_content: str,
    notification_blocks: List[Dict[str, Any]],
    sender_type: str,
) -> Tuple[str, List[Any]]:
    record = f"cs.metadata->'{HUMAN_ASSIST_RECORD_KEY}'"
    query = f"""
        WITH claimed AS (
            UPDATE chat_session cs
            SET metadata = jsonb_set(
                    cs.metadata,
                    '{{{HUMAN_ASSIST_RECORD_KEY}}}',
                    {record} || jsonb_build_object(
                        'status', 'OPEN',
                        'opened_at', COALESCE(
                            NULLIF({record}->>'opened_at', '')::timestamptz,
                            now()
                        ),
                        'opened_by', COALESCE(
                            NULLIF({record}->>'opened_by', ''),
                            $2::text
                        )
                    ),
                    FALSE
                ),
                last_activity_at = now()
            WHERE cs.id = $1::uuid
              AND {record}->>'status' = 'PENDING'
              AND {record}->>'claim_deadline_at' > {_utc_timestamp("now()")}
            RETURNING cs.*
        ),
        notification AS (
            INSERT INTO chat_message (
                session_id,
                idx,
                role,
                content,
                content_blocks,
                sender_type
            )
            SELECT
                claimed.id,
                COALESCE(
                    (
                        SELECT MAX(message.idx) + 1
                        FROM chat_message message
                        WHERE message.session_id = claimed.id
                    ),
                    0
                ),
                'assistant',
                $3,
                $4::jsonb,
                $5
            FROM claimed
            RETURNING session_id
        )
        SELECT {_columns("claimed")}
        FROM claimed
        JOIN notification ON notification.session_id = claimed.id
    """
    return query, [
        conversation_id,
        opened_by,
        notification_content,
        json.dumps(notification_blocks),
        sender_type,
    ]


def touch_human_assist_customer_query(
    chat_session_id: str,
    *,
    mark_activity: bool,
) -> Tuple[str, List[Any]]:
    record = f"cs.metadata->'{HUMAN_ASSIST_RECORD_KEY}'"
    query = f"""
        UPDATE chat_session cs
        SET metadata = jsonb_set(
                cs.metadata,
                '{{{HUMAN_ASSIST_RECORD_KEY}}}',
                {record} || jsonb_build_object(
                    'customer_last_seen_at', {_utc_timestamp("now()")}
                ),
                FALSE
            ),
            last_activity_at = CASE
                WHEN $2::boolean THEN now()
                ELSE cs.last_activity_at
            END
        WHERE cs.id = $1::uuid
          AND {record}->>'status' IN ('PENDING', 'OPEN')
        RETURNING {_columns("cs")}
    """
    return query, [chat_session_id, mark_activity]


def insert_human_assist_platform_message_query(
    *,
    session_id: str,
    role: str,
    content: str,
    content_blocks: List[Dict[str, Any]],
    sender_type: Optional[str],
) -> Tuple[str, List[Any]]:
    """Insert one provider-normalized message.

    No dedup key exists yet (the only shipped adapter, native, never
    supplies an external id) — add one via a migration when a real
    webhook-based adapter needs it. A genuine ``(session_id, idx)``
    primary-key race (concurrent inserts computing the same next
    ``idx``) still raises; callers retry with a fresh ``idx``.
    """
    query = """
        INSERT INTO chat_message (
            session_id,
            idx,
            role,
            content,
            content_blocks,
            sender_type
        )
        VALUES (
            $1::uuid,
            COALESCE(
                (
                    SELECT MAX(message.idx) + 1
                    FROM chat_message message
                    WHERE message.session_id = $1::uuid
                ),
                0
            ),
            $2,
            $3,
            $4::jsonb,
            $5
        )
        RETURNING session_id, idx, role, content, created_at,
                  content_blocks, NULL::jsonb AS ui_blocks, sender_type
    """
    return query, [
        session_id,
        role,
        content,
        json.dumps(content_blocks),
        sender_type,
    ]


def touch_human_assist_activity_query(
    conversation_id: str,
    opened_by: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    record = f"cs.metadata->'{HUMAN_ASSIST_RECORD_KEY}'"
    owner_clause = f"AND {record}->>'opened_by' = $2" if opened_by is not None else ""
    values: List[Any] = [conversation_id]
    if opened_by is not None:
        values.append(opened_by)
    query = f"""
        UPDATE chat_session cs
        SET last_activity_at = now()
        WHERE cs.id = $1::uuid
          AND {record}->>'status' = 'OPEN'
          {owner_clause}
        RETURNING {_columns("cs")}
    """
    return query, values


def close_human_assist_conversation_query(
    conversation_id: str,
    *,
    terminal_status: str,
    close_reason: str,
    closed_by: Optional[str],
    allowed_statuses: List[str],
    notification_content: Optional[str],
    notification_blocks: Optional[List[Dict[str, Any]]],
    notification_sender_type: Optional[str],
    end_session: bool,
) -> Tuple[str, List[Any]]:
    record = f"cs.metadata->'{HUMAN_ASSIST_RECORD_KEY}'"
    query = f"""
        WITH closed AS (
            UPDATE chat_session cs
            SET metadata = jsonb_set(
                    cs.metadata,
                    '{{{HUMAN_ASSIST_RECORD_KEY}}}',
                    {record} || jsonb_build_object(
                        'status', $2::text,
                        'closed_at', COALESCE(
                            NULLIF({record}->>'closed_at', '')::timestamptz,
                            now()
                        ),
                        'closed_by', COALESCE(
                            NULLIF({record}->>'closed_by', ''),
                            $3::text
                        ),
                        'close_reason', COALESCE(
                            NULLIF({record}->>'close_reason', ''),
                            $4::text
                        )
                    ),
                    FALSE
                ),
                status = CASE
                    WHEN $9::boolean THEN 'ENDED'
                    ELSE cs.status
                END,
                current_channel = CASE
                    WHEN $9::boolean OR cs.status = 'ENDED' THEN 'ENDED'
                    ELSE 'CHAT'
                END,
                ended_at = CASE
                    WHEN $9::boolean THEN COALESCE(cs.ended_at, now())
                    ELSE cs.ended_at
                END,
                ended_reason = CASE
                    WHEN $9::boolean THEN COALESCE(cs.ended_reason, 'user_ended')
                    ELSE cs.ended_reason
                END,
                last_activity_at = now()
            WHERE cs.id = $1::uuid
              AND {record}->>'status' = ANY($5::text[])
            RETURNING cs.*
        ),
        notification AS (
            INSERT INTO chat_message (
                session_id,
                idx,
                role,
                content,
                content_blocks,
                sender_type
            )
            SELECT
                closed.id,
                COALESCE(
                    (
                        SELECT MAX(message.idx) + 1
                        FROM chat_message message
                        WHERE message.session_id = closed.id
                    ),
                    0
                ),
                'assistant',
                $6,
                $7::jsonb,
                $8
            FROM closed
            WHERE $6::text IS NOT NULL
            RETURNING session_id
        )
        SELECT {_columns("closed")}
        FROM closed
    """
    return query, [
        conversation_id,
        terminal_status,
        closed_by,
        close_reason,
        allowed_statuses,
        notification_content,
        json.dumps(notification_blocks) if notification_blocks is not None else None,
        notification_sender_type,
        end_session,
    ]


def list_due_human_assist_query(
    *,
    claim_deadline_before: Optional[datetime] = None,
    customer_seen_before: Optional[datetime] = None,
    limit: int = 100,
    exclude_ids: Optional[List[str]] = None,
) -> Tuple[str, List[Any]]:
    status = f"cs.metadata #>> '{{{HUMAN_ASSIST_RECORD_KEY},status}}'"
    claim_deadline = (
        f"cs.metadata #>> '{{{HUMAN_ASSIST_RECORD_KEY},claim_deadline_at}}'"
    )
    customer_last_seen = (
        f"cs.metadata #>> '{{{HUMAN_ASSIST_RECORD_KEY},customer_last_seen_at}}'"
    )
    exclusion = "AND NOT (cs.id = ANY($2::uuid[]))" if exclude_ids else ""
    limit_placeholder = "$3" if exclude_ids else "$2"
    values: List[Any]
    if claim_deadline_before is not None:
        query = f"""
            SELECT {_columns("cs")}
            FROM chat_session cs
            WHERE {status} = 'PENDING'
              AND {claim_deadline} <= {_utc_timestamp("$1::timestamptz")}
              {exclusion}
            ORDER BY {claim_deadline} ASC
            LIMIT {limit_placeholder}
        """
        values = [claim_deadline_before]
        if exclude_ids:
            values.append(exclude_ids)
        values.append(limit)
        return query, values

    query = f"""
        SELECT {_columns("cs")}
        FROM chat_session cs
        WHERE {status} IN ('PENDING', 'OPEN')
          AND {customer_last_seen} <= {_utc_timestamp("$1::timestamptz")}
          {exclusion}
        ORDER BY {customer_last_seen} ASC
        LIMIT {limit_placeholder}
    """
    values = [customer_seen_before]
    if exclude_ids:
        values.append(exclude_ids)
    values.append(limit)
    return query, values
