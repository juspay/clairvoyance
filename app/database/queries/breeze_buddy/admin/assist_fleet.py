"""Read-only cross-table queries behind ``GET /admin/assist-fleet``.

The fleet view joins four tables that each keep their writes in their own
query module (``widget_config.py``, ``template.py``, ``chat_session.py``,
``merchants.py``). Everything here is a SELECT, and every aggregate follows
the ``chat_analytics.py`` rule: per-session message counts come from a
scalar subquery so a LEFT JOIN can never fan a session out by its message
count and skew an average.

``$N::uuid[]`` / ``$N::text[]`` casts are deliberate — asyncpg infers array
element codecs from the cast, so callers pass plain Python lists of ``str``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Sequence, Tuple

WIDGET_CONFIG_TABLE = "widget_config"
TEMPLATE_TABLE = "template"
MERCHANTS_TABLE = "merchants"
CHAT_SESSION_TABLE = "chat_session"
CHAT_MESSAGE_TABLE = "chat_message"

# Day buckets follow the console's analytics convention (IST), not UTC.
_DAY_BUCKET = "(cs.created_at AT TIME ZONE 'Asia/Kolkata')::date::text"


def all_widget_configs_query() -> Tuple[str, List[Any]]:
    """Every widget_config row, active or not — the fleet's one-row-per-agent spine.

    ``public_widget_key`` is intentionally not selected: the fleet payload is an
    inventory, and the key has no business in it.
    """
    query = f"""
        SELECT id, reseller_id, merchant_id, template_id, allowed_origins,
               max_sessions_per_ip_hour, max_messages_per_ip_hour,
               max_concurrent_per_ip, max_voice_sessions_per_ip_hour,
               active, appearance, created_at, updated_at
        FROM {WIDGET_CONFIG_TABLE}
        ORDER BY created_at DESC
    """
    return query, []


def chat_templates_query(
    reseller_ids: Sequence[str], template_ids: Sequence[str]
) -> Tuple[str, List[Any]]:
    """Chat-agent templates under the assist resellers, plus any explicit ids.

    "Chat agent" is the console rule (loom ``agent-type.ts``): ``'chat'`` in
    ``supported_channels``. The explicit id list carries the widget-bound
    templates of *other* resellers (demo / internal tenants) so a widget can
    always be described, even when its template is not an assist template.
    Full ``flow`` + ``configurations`` are selected because generation
    classification needs the prompt and the LLM/tool config.
    """
    query = f"""
        SELECT id, reseller_id, merchant_id, name, is_active, supported_channels,
               flow, configurations, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE (reseller_id = ANY($1::text[]) AND 'chat' = ANY(supported_channels))
           OR id = ANY($2::uuid[])
        ORDER BY name
    """
    return query, [list(reseller_ids), list(template_ids)]


def voice_template_counts_query(reseller_ids: Sequence[str]) -> Tuple[str, List[Any]]:
    """Active telephony (voice-only) templates per merchant under the assist resellers."""
    query = f"""
        SELECT reseller_id, merchant_id, COUNT(*) AS voice_templates
        FROM {TEMPLATE_TABLE}
        WHERE reseller_id = ANY($1::text[])
          AND NOT ('chat' = ANY(supported_channels))
          AND is_active = TRUE
        GROUP BY reseller_id, merchant_id
    """
    return query, [list(reseller_ids)]


def blueprint_templates_query(
    reseller_ids: Sequence[str], name: str
) -> Tuple[str, List[Any]]:
    """Reseller-level blueprint rows (``merchant_id IS NULL``) by exact name."""
    query = f"""
        SELECT id, reseller_id, flow
        FROM {TEMPLATE_TABLE}
        WHERE reseller_id = ANY($1::text[])
          AND merchant_id IS NULL
          AND name = $2
    """
    return query, [list(reseller_ids), name]


def merchants_by_ids_query(merchant_ids: Sequence[str]) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT merchant_id, name, is_active, reseller_id, created_at
        FROM {MERCHANTS_TABLE}
        WHERE merchant_id = ANY($1::text[])
    """
    return query, [list(merchant_ids)]


def template_session_stats_query(
    template_ids: Sequence[str], since_window: datetime, since_7d: datetime
) -> Tuple[str, List[Any]]:
    """Lifetime / window / 7-day session counts and activity bounds per template."""
    query = f"""
        SELECT cs.template_id::text AS template_id,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE cs.created_at >= $2::timestamptz) AS total_window,
               COUNT(*) FILTER (WHERE cs.created_at >= $3::timestamptz) AS total_7d,
               COUNT(*) FILTER (WHERE cs.status = 'ACTIVE') AS active_now,
               MAX(cs.last_activity_at) AS last_activity_at,
               MIN(cs.created_at) AS first_seen_at
        FROM {CHAT_SESSION_TABLE} cs
        WHERE cs.template_id = ANY($1::uuid[])
        GROUP BY cs.template_id
    """
    return query, [list(template_ids), since_window, since_7d]


def template_session_depth_query(
    template_ids: Sequence[str], since_window: datetime
) -> Tuple[str, List[Any]]:
    """Average messages per session and zero-message sessions inside the window.

    Message counts are a scalar subquery per session (see module docstring).
    """
    query = f"""
        WITH s AS (
            SELECT cs.template_id,
                   (SELECT COUNT(*) FROM {CHAT_MESSAGE_TABLE} m
                     WHERE m.session_id = cs.id) AS msgs
            FROM {CHAT_SESSION_TABLE} cs
            WHERE cs.template_id = ANY($1::uuid[])
              AND cs.created_at >= $2::timestamptz
        )
        SELECT template_id::text AS template_id,
               COUNT(*) AS sessions,
               AVG(msgs)::float AS avg_messages,
               COUNT(*) FILTER (WHERE msgs = 0) AS zero_message_sessions
        FROM s
        GROUP BY template_id
    """
    return query, [list(template_ids), since_window]


def template_daily_sessions_query(
    template_ids: Sequence[str], since_window: datetime
) -> Tuple[str, List[Any]]:
    """Sessions per IST calendar day per template inside the window."""
    query = f"""
        SELECT cs.template_id::text AS template_id,
               {_DAY_BUCKET} AS day,
               COUNT(*) AS sessions
        FROM {CHAT_SESSION_TABLE} cs
        WHERE cs.template_id = ANY($1::uuid[])
          AND cs.created_at >= $2::timestamptz
        GROUP BY 1, 2
    """
    return query, [list(template_ids), since_window]


def merchant_session_stats_query(
    reseller_ids: Sequence[str],
    merchant_ids: Sequence[str],
    since_window: datetime,
    since_7d: datetime,
) -> Tuple[str, List[Any]]:
    """Session counts per (reseller, merchant) across every template the merchant
    ever used — survives template swaps, which the per-template stats do not."""
    query = f"""
        SELECT cs.reseller_id, cs.merchant_id,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE cs.created_at >= $3::timestamptz) AS total_window,
               COUNT(*) FILTER (WHERE cs.created_at >= $4::timestamptz) AS total_7d,
               MAX(cs.last_activity_at) AS last_activity_at
        FROM {CHAT_SESSION_TABLE} cs
        WHERE cs.reseller_id = ANY($1::text[])
          AND cs.merchant_id = ANY($2::text[])
        GROUP BY cs.reseller_id, cs.merchant_id
    """
    return query, [list(reseller_ids), list(merchant_ids), since_window, since_7d]


__all__ = [
    "all_widget_configs_query",
    "blueprint_templates_query",
    "chat_templates_query",
    "merchant_session_stats_query",
    "merchants_by_ids_query",
    "template_daily_sessions_query",
    "template_session_depth_query",
    "template_session_stats_query",
    "voice_template_counts_query",
]
