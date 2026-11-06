"""Row → Pydantic decoder for token_registry (migrations 086–088)."""

from app.schemas.breeze_buddy.token_registry import TokenRegistryEntry


def decode_token_registry_entry(row) -> TokenRegistryEntry:
    """Build TokenRegistryEntry from a token_registry asyncpg Record."""
    return TokenRegistryEntry(
        id=str(row["id"]),
        token_hash=row["token_hash"],
        kind=row["kind"],
        source=row["source"],
        subject=row["subject"],
        username=row["username"],
        role=row["role"],
        reseller_ids=list(row["reseller_ids"]),
        merchant_ids=list(row["merchant_ids"]),
        created_by=row["created_by"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
        revoke_reason=row["revoke_reason"],
        last_seen_at=row["last_seen_at"],
        last_seen_from=row["last_seen_from"],
        last_route=row["last_route"],
        replaced_by=str(row["replaced_by"]) if row["replaced_by"] else None,
    )
