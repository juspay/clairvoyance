"""Async accessor functions for token_registry (migrations 086, 087).

Thin wrappers around ``run_parameterized_query`` + the decoder. Errors are
logged and re-raised: a mint whose row was not written must not hand out
its token, and a verifier that cannot read the registry must refuse (see
``rbac_token.create_registered_token`` / ``authenticate``).
"""

from datetime import datetime
from typing import List, Optional, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.token_registry import (
    decode_token_registry_entry,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.token_registry import (
    get_token_by_hash_query,
    get_token_by_id_query,
    list_tokens_query,
    mark_token_seen_query,
    register_token_query,
    revoke_token_query,
)
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


async def get_token_by_hash(token_hash: str) -> Optional[TokenRegistryEntry]:
    """The registry row for one presented token, or None if never seen."""
    query, values = get_token_by_hash_query(token_hash)
    try:
        result = await run_parameterized_query(query, values)
        return decode_token_registry_entry(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error reading token_registry by hash: {e}")
        raise


async def get_token_by_id(token_id: str) -> Optional[TokenRegistryEntry]:
    """One registry row by id (= jti), or None."""
    query, values = get_token_by_id_query(token_id)
    try:
        result = await run_parameterized_query(query, values)
        return decode_token_registry_entry(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error reading token_registry row {token_id}: {e}")
        raise


async def find_or_register_seen_token(
    *,
    token_id: str,
    token_hash: str,
    kind: TokenKind,
    subject: str,
    username: str,
    role: str,
    reseller_ids: List[str],
    merchant_ids: List[str],
    issued_at: datetime,
    expires_at: datetime,
) -> TokenRegistryEntry:
    """The row for a token a verifier just accepted; registered as
    ``backfilled`` the first time it is seen. Raises on any DB error — and
    when no row can be found or written, which leaves the caller nothing to
    decide on."""
    existing = await get_token_by_hash(token_hash)
    if existing:
        return existing
    query, values = register_token_query(
        token_id=token_id,
        token_hash=token_hash,
        kind=kind,
        source="backfilled",
        subject=subject,
        username=username,
        role=role,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        created_by=None,
        issued_at=issued_at,
        expires_at=expires_at,
        if_absent=True,
    )
    try:
        result = await run_parameterized_query(query, values)
    except Exception as e:
        logger.error(f"Error registering seen {kind} token {token_id}: {e}")
        raise
    if result:
        logger.info(f"Registered backfilled {kind} token {token_id} for {username}")
        return decode_token_registry_entry(result[0])
    # Another pod registered it between the read and the insert.
    raced = await get_token_by_hash(token_hash)
    if raced is None:
        raise RuntimeError(
            f"token_registry row for {token_id} neither found nor written"
        )
    return raced


async def mark_token_seen(
    token_id: str, seen_from: Optional[str], route: Optional[str]
) -> Optional[datetime]:
    """Stamp last_seen_*; returns the stamp (None if the row is gone)."""
    query, values = mark_token_seen_query(token_id, seen_from, route)
    try:
        result = await run_parameterized_query(query, values)
        return result[0]["last_seen_at"] if result else None
    except Exception as e:
        logger.error(f"Error stamping last_seen on token {token_id}: {e}")
        raise


async def list_tokens(
    *,
    kind: Optional[TokenKind],
    merchant_id: Optional[str],
    include_revoked: bool,
    limit: int,
    offset: int,
) -> List[TokenRegistryEntry]:
    """Registry rows, newest first."""
    query, values = list_tokens_query(
        kind=kind,
        merchant_id=merchant_id,
        include_revoked=include_revoked,
        limit=limit,
        offset=offset,
    )
    try:
        result = await run_parameterized_query(query, values)
        return [decode_token_registry_entry(row) for row in result]
    except Exception as e:
        logger.error(f"Error listing token_registry: {e}")
        raise


async def revoke_token(
    token_id: str, revoked_by: str, reason: str
) -> Optional[Tuple[TokenRegistryEntry, int]]:
    """Revoke a live token and clear any merchant s2s_token holding it.

    Returns (row, merchant tokens cleared), or None when the id is unknown
    or already revoked — the caller re-reads to tell which.
    """
    query, values = revoke_token_query(token_id, revoked_by, reason)
    try:
        result = await run_parameterized_query(query, values)
        if not result:
            return None
        row = result[0]
        logger.warning(
            f"Revoked token {token_id} by {revoked_by} "
            f"(reason: {reason}, merchant tokens cleared: {row['merchant_tokens_cleared']})"
        )
        return decode_token_registry_entry(row), int(row["merchant_tokens_cleared"])
    except Exception as e:
        logger.error(f"Error revoking token {token_id}: {e}")
        raise
