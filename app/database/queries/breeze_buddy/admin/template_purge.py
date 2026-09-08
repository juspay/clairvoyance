"""Queries behind ``DELETE /admin/templates/{id}/purge``.

A template with chat history cannot be deleted through the tenant endpoint:
``chat_session.template_id`` is ``ON DELETE RESTRICT`` and nothing deletes
sessions. The purge removes the sessions first (``chat_message``,
``agent_session_state``, ``chat_turn_metrics`` and ``tool_approvals`` cascade
from the session) and then the template, in one transaction. Anything that
would still point at the template afterwards — a widget_config, a call
execution config, an in-flight lead — blocks the purge instead.
"""

from __future__ import annotations

from typing import Any, List, Tuple

TEMPLATE_TABLE = "template"
WIDGET_CONFIG_TABLE = "widget_config"
CALL_CONFIG_TABLE = "call_execution_config"
LEAD_TABLE = "lead_call_tracker"
CHAT_SESSION_TABLE = "chat_session"
CHAT_MESSAGE_TABLE = "chat_message"

INFLIGHT_LEAD_STATUSES = ("BACKLOG", "RETRY", "PROCESSING")


def template_purge_blockers_query(template_id: str) -> Tuple[str, List[Any]]:
    """One row of counts: what blocks the purge and what it would remove."""
    statuses = ", ".join(f"'{s}'" for s in INFLIGHT_LEAD_STATUSES)
    query = f"""
        SELECT
            (SELECT COUNT(*) FROM {WIDGET_CONFIG_TABLE}
              WHERE template_id = $1::uuid)                          AS widget_configs,
            (SELECT COUNT(*) FROM {CALL_CONFIG_TABLE}
              WHERE template_id = $1::uuid)                          AS call_configs,
            (SELECT COUNT(*) FROM {LEAD_TABLE}
              WHERE template_id = $1::uuid AND status IN ({statuses})) AS inflight_leads,
            (SELECT COUNT(*) FROM {CHAT_SESSION_TABLE}
              WHERE template_id = $1::uuid)                          AS chat_sessions,
            (SELECT COUNT(*) FROM {CHAT_SESSION_TABLE}
              WHERE template_id = $1::uuid AND status = 'ACTIVE')     AS active_sessions,
            (SELECT COUNT(*) FROM {CHAT_MESSAGE_TABLE} m
              JOIN {CHAT_SESSION_TABLE} s ON s.id = m.session_id
              WHERE s.template_id = $1::uuid)                        AS chat_messages
    """
    return query, [template_id]


def delete_template_sessions_query(template_id: str) -> Tuple[str, List[Any]]:
    """Delete every chat session of the template; dependents cascade."""
    query = f"""
        DELETE FROM {CHAT_SESSION_TABLE}
        WHERE template_id = $1::uuid
        RETURNING id
    """
    return query, [template_id]


def delete_template_row_query(template_id: str) -> Tuple[str, List[Any]]:
    """Delete the template row itself (run after the sessions, same transaction)."""
    query = f"""
        DELETE FROM {TEMPLATE_TABLE}
        WHERE id = $1::uuid
        RETURNING id, reseller_id, merchant_id, name, is_active, created_at, updated_at
    """
    return query, [template_id]


__all__ = [
    "INFLIGHT_LEAD_STATUSES",
    "delete_template_row_query",
    "delete_template_sessions_query",
    "template_purge_blockers_query",
]
