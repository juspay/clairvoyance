"""SQL builders for ui_component (migration 057).

Pure functions; no DB I/O. Returns ``Tuple[str, List[Any]]`` for
``run_parameterized_query``. JSONB values arrive as JSON strings and are
cast with ``::jsonb`` (same convention as the template queries).
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

UI_COMPONENT_TABLE = "ui_component"

_COLUMNS = (
    "id, reseller_id, merchant_id, name, version, props_schema, flags, "
    "render_def, prompt_hint, is_active, created_at, updated_at"
)


def create_ui_component_query(
    *,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    props_schema: str,
    flags: str,
    render_def: Optional[str],
    prompt_hint: Optional[str],
    is_active: bool,
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO {UI_COMPONENT_TABLE} (
            reseller_id, merchant_id, name, props_schema, flags,
            render_def, prompt_hint, is_active, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8, $9, $10
        )
        RETURNING {_COLUMNS}
    """
    now = datetime.now(timezone.utc)
    return query, [
        reseller_id,
        merchant_id,
        name,
        props_schema,
        flags,
        render_def,
        prompt_hint,
        bool(is_active),
        now,
        now,
    ]


def get_ui_component_by_id_query(ui_component_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_COLUMNS}
        FROM {UI_COMPONENT_TABLE}
        WHERE id = $1::uuid
    """
    return query, [ui_component_id]


def get_ui_components_by_names_query(
    *,
    reseller_id: str,
    merchant_id: Optional[str],
    names: List[str],
) -> Tuple[str, List[Any]]:
    """Session-start def fetch: active rows matching the template's opt-in
    names, scoped to the template's reseller — merchant rows win over
    reseller-wide rows of the same name (ORDER + DISTINCT ON)."""
    query = f"""
        SELECT DISTINCT ON (name) {_COLUMNS}
        FROM {UI_COMPONENT_TABLE}
        WHERE reseller_id = $1
          AND name = ANY($2)
          AND is_active = TRUE
          AND (merchant_id IS NULL OR merchant_id = $3)
        ORDER BY name, merchant_id NULLS LAST
    """
    return query, [reseller_id, list(names), merchant_id or ""]


def list_ui_components_query(
    *,
    page: int = 1,
    limit: int = 50,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> Tuple[str, str, List[Any]]:
    """Returns (query, count_query, values) — same composition rules as
    ``list_widget_configs_query``."""
    where: List[str] = []
    params: List[Any] = []
    idx = 1

    if reseller_id:
        where.append(f"reseller_id = ${idx}")
        params.append(reseller_id)
        idx += 1
    elif reseller_ids is not None:
        where.append(f"reseller_id = ANY(${idx})")
        params.append(reseller_ids)
        idx += 1

    if merchant_id:
        where.append(f"merchant_id = ${idx}")
        params.append(merchant_id)
        idx += 1

    if not include_inactive:
        where.append("is_active = TRUE")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    offset = max(page - 1, 0) * limit

    query = f"""
        SELECT {_COLUMNS}
        FROM {UI_COMPONENT_TABLE}
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """
    count_query = f"SELECT COUNT(*) AS total FROM {UI_COMPONENT_TABLE} {where_clause}"
    params.extend([limit, offset])
    return query, count_query, params


def update_ui_component_query(
    *,
    ui_component_id: str,
    props_schema: Optional[str] = None,
    flags: Optional[str] = None,
    render_def: Optional[str] = None,
    prompt_hint: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """Returns ("", []) when no fields were provided. Every real update
    bumps ``version`` — hydrated ops and def caches key on it."""
    set_clauses: List[str] = []
    params: List[Any] = []
    idx = 1

    if props_schema is not None:
        set_clauses.append(f"props_schema = ${idx}::jsonb")
        params.append(props_schema)
        idx += 1
    if flags is not None:
        set_clauses.append(f"flags = ${idx}::jsonb")
        params.append(flags)
        idx += 1
    if render_def is not None:
        set_clauses.append(f"render_def = ${idx}::jsonb")
        params.append(render_def)
        idx += 1
    if prompt_hint is not None:
        set_clauses.append(f"prompt_hint = ${idx}")
        params.append(prompt_hint)
        idx += 1
    if is_active is not None:
        set_clauses.append(f"is_active = ${idx}")
        params.append(bool(is_active))
        idx += 1

    if not set_clauses:
        return "", []

    set_clauses.append("version = version + 1")
    set_clauses.append(f"updated_at = ${idx}")
    params.append(datetime.now(timezone.utc))
    idx += 1

    params.append(ui_component_id)
    query = f"""
        UPDATE {UI_COMPONENT_TABLE}
        SET {', '.join(set_clauses)}
        WHERE id = ${idx}::uuid
        RETURNING {_COLUMNS}
    """
    return query, params


def delete_ui_component_query(ui_component_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        DELETE FROM {UI_COMPONENT_TABLE}
        WHERE id = $1::uuid
        RETURNING id
    """
    return query, [ui_component_id]
