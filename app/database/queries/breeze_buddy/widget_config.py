"""SQL builders for widget_config (migration 029).

Pure functions; no DB I/O. Returns ``Tuple[str, List[Any]]`` for
``run_parameterized_query``. All placeholders are PostgreSQL-style
``$N`` — never use string formatting for user input.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

WIDGET_CONFIG_TABLE = "widget_config"

_COLUMNS = (
    "id, reseller_id, merchant_id, public_widget_key, template_id, "
    "allowed_origins, max_sessions_per_ip_hour, max_messages_per_ip_hour, "
    "max_concurrent_per_ip, max_voice_sessions_per_ip_hour, active, "
    "created_at, updated_at"
)


def create_widget_config_query(
    *,
    reseller_id: str,
    merchant_id: str,
    public_widget_key: str,
    template_id: str,
    allowed_origins: List[str],
    max_sessions_per_ip_hour: int,
    max_messages_per_ip_hour: int,
    max_concurrent_per_ip: int,
    max_voice_sessions_per_ip_hour: int,
    active: bool,
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO {WIDGET_CONFIG_TABLE} (
            reseller_id, merchant_id, public_widget_key, template_id,
            allowed_origins, max_sessions_per_ip_hour, max_messages_per_ip_hour,
            max_concurrent_per_ip, max_voice_sessions_per_ip_hour,
            active, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11, $12
        )
        RETURNING {_COLUMNS}
    """
    now = datetime.now(timezone.utc)
    values: List[Any] = [
        reseller_id,
        merchant_id,
        public_widget_key,
        template_id,
        list(allowed_origins),
        int(max_sessions_per_ip_hour),
        int(max_messages_per_ip_hour),
        int(max_concurrent_per_ip),
        int(max_voice_sessions_per_ip_hour),
        bool(active),
        now,
        now,
    ]
    return query, values


def get_widget_config_by_id_query(widget_config_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_COLUMNS}
        FROM {WIDGET_CONFIG_TABLE}
        WHERE id = $1::uuid
    """
    return query, [widget_config_id]


def get_widget_config_by_public_key_query(
    public_widget_key: str,
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_COLUMNS}
        FROM {WIDGET_CONFIG_TABLE}
        WHERE public_widget_key = $1
    """
    return query, [public_widget_key]


def get_widget_config_by_reseller_merchant_query(
    reseller_id: str, merchant_id: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_COLUMNS}
        FROM {WIDGET_CONFIG_TABLE}
        WHERE reseller_id = $1 AND merchant_id = $2
    """
    return query, [reseller_id, merchant_id]


def list_widget_configs_query(
    *,
    page: int = 1,
    limit: int = 50,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> Tuple[str, str, List[Any]]:
    """Returns (query, count_query, values).

    Filters compose like templates: exact reseller_id / merchant_id when
    supplied, otherwise the optional ``reseller_ids`` / ``merchant_ids``
    arrays scope the result to the caller's RBAC reach.
    """
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
        where.append(f"merchant_id = ANY(${idx})")
        params.append(merchant_ids)
        idx += 1

    if not include_inactive:
        where.append("active = TRUE")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    offset = max(page - 1, 0) * limit

    query = f"""
        SELECT {_COLUMNS}
        FROM {WIDGET_CONFIG_TABLE}
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """
    count_query = f"SELECT COUNT(*) AS total FROM {WIDGET_CONFIG_TABLE} {where_clause}"
    params.extend([limit, offset])
    return query, count_query, params


def update_widget_config_query(
    *,
    widget_config_id: str,
    template_id: Optional[str] = None,
    allowed_origins: Optional[List[str]] = None,
    max_sessions_per_ip_hour: Optional[int] = None,
    max_messages_per_ip_hour: Optional[int] = None,
    max_concurrent_per_ip: Optional[int] = None,
    max_voice_sessions_per_ip_hour: Optional[int] = None,
    active: Optional[bool] = None,
) -> Tuple[str, List[Any]]:
    """Returns ("", []) if no fields were provided — callers should treat
    that as a no-op and return the current row."""
    set_clauses: List[str] = []
    params: List[Any] = []
    idx = 1

    if template_id is not None:
        set_clauses.append(f"template_id = ${idx}::uuid")
        params.append(template_id)
        idx += 1

    if allowed_origins is not None:
        set_clauses.append(f"allowed_origins = ${idx}")
        params.append(list(allowed_origins))
        idx += 1

    if max_sessions_per_ip_hour is not None:
        set_clauses.append(f"max_sessions_per_ip_hour = ${idx}")
        params.append(int(max_sessions_per_ip_hour))
        idx += 1

    if max_messages_per_ip_hour is not None:
        set_clauses.append(f"max_messages_per_ip_hour = ${idx}")
        params.append(int(max_messages_per_ip_hour))
        idx += 1

    if max_concurrent_per_ip is not None:
        set_clauses.append(f"max_concurrent_per_ip = ${idx}")
        params.append(int(max_concurrent_per_ip))
        idx += 1

    if max_voice_sessions_per_ip_hour is not None:
        set_clauses.append(f"max_voice_sessions_per_ip_hour = ${idx}")
        params.append(int(max_voice_sessions_per_ip_hour))
        idx += 1

    if active is not None:
        set_clauses.append(f"active = ${idx}")
        params.append(bool(active))
        idx += 1

    if not set_clauses:
        return "", []

    set_clauses.append(f"updated_at = ${idx}")
    params.append(datetime.now(timezone.utc))
    idx += 1

    params.append(widget_config_id)
    query = f"""
        UPDATE {WIDGET_CONFIG_TABLE}
        SET {', '.join(set_clauses)}
        WHERE id = ${idx}::uuid
        RETURNING {_COLUMNS}
    """
    return query, params


def delete_widget_config_query(widget_config_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        DELETE FROM {WIDGET_CONFIG_TABLE}
        WHERE id = $1::uuid
        RETURNING id
    """
    return query, [widget_config_id]
