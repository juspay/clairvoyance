"""Accessors behind ``DELETE /admin/templates/{id}/purge``."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.logger import logger
from app.database import get_db_connection
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.admin.template_purge import (
    delete_template_row_query,
    delete_template_sessions_query,
    template_purge_blockers_query,
)

BLOCKER_FIELDS = (
    "widget_configs",
    "call_configs",
    "inflight_leads",
    "chat_sessions",
    "active_sessions",
    "chat_messages",
)


class _TemplateGone(Exception):
    """Raised inside the transaction so the session deletes roll back."""


async def fetch_template_purge_blockers(template_id: str) -> Dict[str, int]:
    """Counts of references and of history the purge would remove."""
    query, values = template_purge_blockers_query(template_id)
    result = await run_parameterized_query(query, values)
    row = result[0] if result else {}
    return {field: int(row.get(field) or 0) for field in BLOCKER_FIELDS}


async def purge_template_with_sessions(template_id: str) -> Optional[Dict[str, Any]]:
    """Delete the template's chat sessions and then the template, atomically.

    Returns ``None`` when the template no longer exists (nothing is deleted in
    that case either — the transaction rolls back). A foreign-key violation
    from a reference that appeared after the pre-check also rolls everything
    back and is re-raised.
    """
    sessions_query, sessions_values = delete_template_sessions_query(template_id)
    row_query, row_values = delete_template_row_query(template_id)
    async for conn in get_db_connection():
        async with conn.transaction():
            sessions = await conn.fetch(sessions_query, *sessions_values)
            row = await conn.fetchrow(row_query, *row_values)
            if row is None:
                # Template vanished between pre-check and purge: keep the sessions.
                raise _TemplateGone()
            logger.info(
                f"Purged template {template_id} ({row['name']}) with "
                f"{len(sessions)} chat session(s)"
            )
            return {"template": dict(row), "chat_sessions_deleted": len(sessions)}
    return None


async def purge_template(template_id: str) -> Optional[Dict[str, Any]]:
    """``purge_template_with_sessions`` with the vanished-template case as ``None``."""
    try:
        return await purge_template_with_sessions(template_id)
    except _TemplateGone:
        return None


__all__ = [
    "BLOCKER_FIELDS",
    "fetch_template_purge_blockers",
    "purge_template",
    "purge_template_with_sessions",
]
