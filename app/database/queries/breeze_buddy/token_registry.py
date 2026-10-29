"""SQL builders for token_registry (migration 086).

Pure functions; no DB I/O. Returns ``Tuple[str, List[Any]]`` for
``run_parameterized_query``.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

TOKEN_REGISTRY_TABLE = "token_registry"

_COLUMNS = (
    "id, token_hash, kind, source, subject, username, role, reseller_ids, "
    "merchant_ids, created_by, issued_at, expires_at, created_at"
)


def register_token_query(
    *,
    token_id: str,
    token_hash: str,
    kind: str,
    source: str,
    subject: str,
    username: str,
    role: str,
    reseller_ids: List[str],
    merchant_ids: List[str],
    created_by: Optional[str],
    issued_at: datetime,
    expires_at: datetime,
) -> Tuple[str, List[Any]]:
    """Insert one registry row and return it."""
    query = f"""
        INSERT INTO {TOKEN_REGISTRY_TABLE} (
            id, token_hash, kind, source, subject, username, role,
            reseller_ids, merchant_ids, created_by, issued_at, expires_at
        ) VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
        )
        RETURNING {_COLUMNS}
    """
    return query, [
        token_id,
        token_hash,
        kind,
        source,
        subject,
        username,
        role,
        reseller_ids,
        merchant_ids,
        created_by,
        issued_at,
        expires_at,
    ]
