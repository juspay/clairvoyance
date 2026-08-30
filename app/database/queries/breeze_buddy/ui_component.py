"""SQL builders for ui_component (migration 070).

Pure functions; no DB I/O. Returns ``Tuple[str, List[Any]]`` for
``run_parameterized_query``. JSONB values arrive as JSON strings and are
cast with ``::jsonb`` (same convention as the template queries).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    merchant_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> Tuple[str, str, List[Any]]:
    """Returns (query, count_query, values) — same composition rules as
    ``list_widget_configs_query``, except that the ``merchant_ids``
    allowlist also admits reseller-wide rows (NULL merchant)."""
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
    elif merchant_ids is not None:
        # Reseller-wide rows are visible to anyone with reseller access —
        # the same rule filter_templates_by_rbac applies to templates.
        where.append(f"(merchant_id IS NULL OR merchant_id = ANY(${idx}))")
        params.append(merchant_ids)
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


# Column → parameter cast for UPDATE. This allowlist is what keeps ``patch``
# keys out of the SQL text: unknown keys are ignored, values are always $N.
_UPDATABLE_COLUMNS = {
    "props_schema": "::jsonb",
    "flags": "::jsonb",
    "render_def": "::jsonb",
    "prompt_hint": "",
    "is_active": "",
}


def update_ui_component_query(
    *,
    ui_component_id: str,
    patch: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """``patch`` maps column → new value for ONLY the columns the caller
    supplied; a ``None`` value writes NULL (the handler has already
    refused null for NOT NULL columns). Returns ("", []) when nothing
    updatable was supplied. Every real update bumps ``version`` —
    hydrated ops and def caches key on it."""
    set_clauses: List[str] = []
    params: List[Any] = []
    idx = 1

    for column, cast in _UPDATABLE_COLUMNS.items():
        if column not in patch:
            continue
        set_clauses.append(f"{column} = ${idx}{cast}")
        params.append(patch[column])
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
