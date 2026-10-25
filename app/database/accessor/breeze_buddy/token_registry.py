"""Async accessor functions for token_registry (migration 086).

Thin wrappers around ``run_parameterized_query`` + the decoder. Errors are
logged and re-raised: a mint whose row was not written must not hand out
its token (see ``rbac_token.create_registered_token``).
"""

from datetime import datetime
from typing import List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.token_registry import (
    decode_token_registry_entry,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.token_registry import register_token_query
from app.schemas.breeze_buddy.token_registry import (
    TokenKind,
    TokenRegistryEntry,
    TokenSource,
)


async def register_token(
    *,
    token_id: str,
    token_hash: str,
    kind: TokenKind,
    source: TokenSource,
    subject: str,
    username: str,
    role: str,
    reseller_ids: List[str],
    merchant_ids: List[str],
    created_by: Optional[str],
    issued_at: datetime,
    expires_at: datetime,
) -> TokenRegistryEntry:
    """Write one registry row. Raises if the row was not written (an
    INSERT … RETURNING with no row raises at ``result[0]``)."""
    query, values = register_token_query(
        token_id=token_id,
        token_hash=token_hash,
        kind=kind,
        source=source,
        subject=subject,
        username=username,
        role=role,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        created_by=created_by,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    try:
        result = await run_parameterized_query(query, values)
        entry = decode_token_registry_entry(result[0])
        logger.info(
            f"Registered {kind} token {token_id} for {username} "
            f"(created_by: {created_by}, expires_at: {expires_at.isoformat()})"
        )
        return entry
    except Exception as e:
        logger.error(f"Error registering {kind} token {token_id}: {e}")
        raise
