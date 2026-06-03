"""Aggregate analytics query builders for the chat (text-mode) channel.

Mirrors the voice analytics pattern (``analytics.py``) but over the
``chat_session`` / ``chat_message`` tables — the source for the
Analytics → Chats dashboard (total conversations, total messages, and a
"chats started" time-series, optionally grouped by agent/template).

Date handling is identical to voice (``build_analytics_where_clause``):
``date_from``/``date_to`` are IST calendar dates, converted to UTC, with an
inclusive ``date_to`` (``< next-day midnight``). Reuses ``convert_ist_to_utc``
and ``is_uuid`` from the voice module so the two channels filter consistently.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.database.queries.breeze_buddy.analytics import convert_ist_to_utc, is_uuid

CHAT_SESSION_TABLE = "chat_session"
CHAT_MESSAGE_TABLE = "chat_message"


def build_chat_analytics_where_clause(
    filters: Dict[str, Any], value_offset: int = 0
) -> Tuple[List[str], List[Any]]:
    """WHERE conditions + values for chat_session (aliased ``cs``).

    ``filters`` is the post-RBAC dict (``apply_hierarchical_filters`` already
    injected the caller's accessible reseller/merchant ids). Supports the same
    keys the voice builder does that apply to chat: date range, template_id
    (or ``template`` when it's a UUID), reseller(_id/_ids), merchant(_id/_ids),
    status.
    """
    conditions: List[str] = []
    values: List[Any] = []

    if filters.get("date_from"):
        date_from = filters["date_from"]
        if isinstance(date_from, datetime):
            values.append(convert_ist_to_utc(date_from))
        else:
            values.append(
                convert_ist_to_utc(datetime.combine(date_from, datetime.min.time()))
            )
        conditions.append(f"cs.created_at >= ${len(values) + value_offset}")

    if filters.get("date_to"):
        date_to = filters["date_to"]
        if isinstance(date_to, datetime):
            values.append(convert_ist_to_utc(date_to))
        else:
            next_day = datetime.combine(date_to, datetime.min.time()) + timedelta(
                days=1
            )
            values.append(convert_ist_to_utc(next_day))
        conditions.append(f"cs.created_at < ${len(values) + value_offset}")

    # `template` is a name on voice; chat has no name column, so only honour
    # it when it's actually a UUID. `template_id` is the explicit chat key.
    if filters.get("template") and is_uuid(filters["template"]):
        values.append(filters["template"])
        conditions.append(f"cs.template_id = ${len(values) + value_offset}::UUID")

    if filters.get("template_id"):
        values.append(filters["template_id"])
        conditions.append(f"cs.template_id = ${len(values) + value_offset}::UUID")

    if filters.get("reseller_id"):
        values.append(filters["reseller_id"])
        conditions.append(f"cs.reseller_id = ${len(values) + value_offset}")

    if filters.get("reseller_ids"):
        values.append(filters["reseller_ids"])
        conditions.append(f"cs.reseller_id = ANY(${len(values) + value_offset})")

    if filters.get("merchant_id"):
        values.append(filters["merchant_id"])
        conditions.append(f"cs.merchant_id = ${len(values) + value_offset}")

    if filters.get("merchant_ids"):
        values.append(filters["merchant_ids"])
        conditions.append(f"cs.merchant_id = ANY(${len(values) + value_offset})")

    if filters.get("status"):
        values.append(filters["status"])
        conditions.append(f"cs.status = ${len(values) + value_offset}")

    return conditions, values


# Shared metric columns. COUNT(DISTINCT cs.id) is correct under the
# LEFT JOIN to chat_message (each session row fans out per message);
# COUNT(m.session_id) counts message rows (NULL — sessions with no
# messages — are not counted).
_AGG_METRICS = """
    COUNT(DISTINCT cs.id) AS total_conversations,
    COUNT(DISTINCT cs.id) FILTER (WHERE cs.status = 'ACTIVE') AS active_conversations,
    COUNT(DISTINCT cs.id) FILTER (WHERE cs.status = 'IDLE') AS idle_conversations,
    COUNT(DISTINCT cs.id) FILTER (WHERE cs.status = 'ENDED') AS ended_conversations,
    COUNT(m.session_id) AS total_messages
"""


def get_chat_analytics_summary_query(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """Aggregate chat metrics. ``group_by='template'`` → one row per agent."""
    conditions, values = build_chat_analytics_where_clause(filters)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    if group_by == "template":
        text = f"""
            SELECT
                cs.template_id::text AS template_id,
                {_AGG_METRICS}
            FROM {CHAT_SESSION_TABLE} cs
            LEFT JOIN {CHAT_MESSAGE_TABLE} m ON m.session_id = cs.id
            {where}
            GROUP BY cs.template_id
            ORDER BY total_conversations DESC
        """
    else:
        text = f"""
            SELECT
                {_AGG_METRICS},
                COUNT(DISTINCT cs.template_id) AS total_agents
            FROM {CHAT_SESSION_TABLE} cs
            LEFT JOIN {CHAT_MESSAGE_TABLE} m ON m.session_id = cs.id
            {where}
        """
    return text, values


def get_chat_analytics_trends_query(
    filters: Dict[str, Any], time_granularity: str = "day"
) -> Tuple[str, List[Any]]:
    """Time-bucketed "chats started" series (conversations created per bucket)."""
    conditions, values = build_chat_analytics_where_clause(filters)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    # Whitelisted — never interpolate raw user input into SQL.
    date_trunc = (
        "week"
        if time_granularity == "week"
        else "month" if time_granularity == "month" else "day"
    )
    text = f"""
        SELECT
            DATE_TRUNC('{date_trunc}', cs.created_at) AS time_bucket,
            COUNT(*) AS conversations_started,
            COUNT(*) FILTER (WHERE cs.status = 'ENDED') AS ended_conversations
        FROM {CHAT_SESSION_TABLE} cs
        {where}
        GROUP BY time_bucket
        ORDER BY time_bucket ASC
    """
    return text, values
