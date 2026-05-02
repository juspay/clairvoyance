"""
SQL queries for blueprint session operations.
"""

from typing import Any, List, Optional, Tuple

# Table name constant
BLUEPRINT_SESSIONS_TABLE = "blueprint_sessions"


def create_session_query(
    session_id: str,
    user_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    mode: str,
    template_id: Optional[str],
    langgraph_thread_id: str,
    current_step: Optional[str],
    status: str,
    created_at,
    updated_at,
    expires_at,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new blueprint session."""
    query = f"""
        INSERT INTO {BLUEPRINT_SESSIONS_TABLE} (
            id, user_id, reseller_id, merchant_id, mode, template_id,
            langgraph_thread_id, current_step, status, created_at, updated_at, expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id, user_id, reseller_id, merchant_id, mode, template_id,
                  langgraph_thread_id, current_step, status, result_template_id,
                  created_at, updated_at, expires_at
    """

    return query, [
        session_id,
        user_id,
        reseller_id,
        merchant_id,
        mode,
        template_id,
        langgraph_thread_id,
        current_step,
        status,
        created_at,
        updated_at,
        expires_at,
    ]


def get_session_by_id_query(session_id: str) -> Tuple[str, List[Any]]:
    """Generate query to get a blueprint session by ID."""
    query = f"""
        SELECT id, user_id, reseller_id, merchant_id, mode, template_id,
               langgraph_thread_id, current_step, status, result_template_id,
               created_at, updated_at, expires_at
        FROM {BLUEPRINT_SESSIONS_TABLE}
        WHERE id = $1
        LIMIT 1
    """

    return query, [session_id]


def get_sessions_by_user_query(
    user_id: str, status: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """Generate query to get blueprint sessions by user ID, optionally filtered by status."""
    conditions = ["user_id = $1"]
    values: List[Any] = [user_id]

    if status:
        conditions.append(f"status = ${len(values) + 1}")
        values.append(status)

    query = f"""
        SELECT id, user_id, reseller_id, merchant_id, mode, template_id,
               langgraph_thread_id, current_step, status, result_template_id,
               created_at, updated_at, expires_at
        FROM {BLUEPRINT_SESSIONS_TABLE}
        WHERE {" AND ".join(conditions)}
        ORDER BY created_at DESC
    """

    return query, values


def update_session_query(
    session_id: str,
    current_step: Optional[str],
    status: Optional[str],
    result_template_id: Optional[str],
    updated_at,
) -> Tuple[str, List[Any]]:
    """Generate query to update a blueprint session.

    Only includes columns in the SET clause when their value is not None.
    ``updated_at`` is always included.
    """
    set_clauses: List[str] = []
    values: List[Any] = []

    if current_step is not None:
        values.append(current_step)
        set_clauses.append(f"current_step = ${len(values)}")

    if status is not None:
        values.append(status)
        set_clauses.append(f"status = ${len(values)}")

    if result_template_id is not None:
        values.append(result_template_id)
        set_clauses.append(f"result_template_id = ${len(values)}")

    # updated_at is always set
    values.append(updated_at)
    set_clauses.append(f"updated_at = ${len(values)}")

    # session_id for the WHERE clause
    values.append(session_id)

    query = f"""
        UPDATE {BLUEPRINT_SESSIONS_TABLE}
        SET {", ".join(set_clauses)}
        WHERE id = ${len(values)}
        RETURNING id, user_id, reseller_id, merchant_id, mode, template_id,
                  langgraph_thread_id, current_step, status, result_template_id,
                  created_at, updated_at, expires_at
    """

    return query, values


def delete_session_query(session_id: str) -> Tuple[str, List[Any]]:
    """Generate query to delete a blueprint session."""
    query = f"""
        DELETE FROM {BLUEPRINT_SESSIONS_TABLE}
        WHERE id = $1
        RETURNING id
    """

    return query, [session_id]
