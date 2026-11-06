"""
Database accessor functions for reseller (umbrella) entity management.

Uses queries from queries.breeze_buddy.resellers. Rows map straight onto
ResellerResponse (the computed columns come from the SELECT itself).
"""

from typing import Any, List, Optional, Tuple

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.resellers import (
    create_reseller_query,
    delete_reseller_query,
    get_all_resellers_query,
    get_reseller_by_id_query,
    get_user_umbrella_grants_query,
    get_user_workspace_access_query,
    update_reseller_query,
)
from app.schemas.breeze_buddy.resellers import (
    ResellerResponse,
    UmbrellaGrant,
    WorkspaceAccess,
)


def _decode_reseller(row: Any) -> ResellerResponse:
    return ResellerResponse(
        id=row["id"],
        name=row.get("name"),
        description=row.get("description"),
        is_active=row["is_active"],
        workspace_count=row.get("workspace_count") or 0,
        member_count=row.get("member_count") or 0,
        has_login=row.get("has_login") or False,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def get_reseller_by_id(reseller_id: str) -> Optional[ResellerResponse]:
    """Get one reseller entity with computed columns."""
    query, values = get_reseller_by_id_query(reseller_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return _decode_reseller(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching reseller {reseller_id}: {e}")
        raise


async def get_all_resellers(
    page: int = 1,
    limit: int = 50,
    id_or_name_filter: Optional[str] = None,
    is_active_filter: Optional[bool] = None,
    allowed_reseller_ids: Optional[List[str]] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[List[ResellerResponse], int]:
    """List resellers with pagination and RBAC scoping."""
    query, count_query, values = get_all_resellers_query(
        page=page,
        limit=limit,
        id_or_name_filter=id_or_name_filter,
        is_active_filter=is_active_filter,
        allowed_reseller_ids=allowed_reseller_ids,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    try:
        rows = await run_parameterized_query(query, values)
        count_result = await run_parameterized_query(
            count_query, values[:-2] if len(values) > 2 else []
        )
        count_row = count_result[0] if count_result else None

        resellers = [_decode_reseller(row) for row in rows] if rows else []
        total = count_row["total"] if count_row else 0
        return resellers, total
    except Exception as e:
        logger.error(f"Error fetching resellers: {e}")
        raise


async def create_reseller(
    id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: bool = True,
) -> Optional[ResellerResponse]:
    """Create a reseller entity (no login — that's a users-API concern)."""
    query, values = create_reseller_query(
        id=id, name=name, description=description, is_active=is_active
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            logger.info(f"Created reseller entity: {id}")
            # Fresh rows have no merchants/grants/login yet — decode directly.
            return _decode_reseller(
                dict(row)
                | {"workspace_count": 0, "member_count": 0, "has_login": False}
            )
        return None
    except Exception as e:
        logger.error(f"Error creating reseller {id}: {e}")
        raise


async def update_reseller(
    reseller_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[ResellerResponse]:
    """Update a reseller entity; returns the refreshed row."""
    query, values = update_reseller_query(
        reseller_id=reseller_id,
        name=name,
        description=description,
        is_active=is_active,
    )
    if not values:
        return await get_reseller_by_id(reseller_id)

    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if not row:
            return None
        logger.info(f"Updated reseller entity: {reseller_id}")
        # Re-read for the computed columns.
        return await get_reseller_by_id(reseller_id)
    except Exception as e:
        logger.error(f"Error updating reseller {reseller_id}: {e}")
        raise


async def delete_reseller(reseller_id: str) -> bool:
    """Delete a reseller entity.

    Raises asyncpg.ForeignKeyViolationError (mapped by the handler to 409)
    while merchants still reference the umbrella. Grant rows cascade.
    """
    query, values = delete_reseller_query(reseller_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            logger.info(f"Deleted reseller entity: {reseller_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error deleting reseller {reseller_id}: {e}")
        raise


async def get_user_umbrella_grants(user_id: str) -> List[UmbrellaGrant]:
    """List a user's umbrella grants from the normalized tables."""
    query, values = get_user_umbrella_grants_query(user_id)
    try:
        rows = await run_parameterized_query(query, values)
        return [
            UmbrellaGrant(
                reseller_id=row["reseller_id"],
                reseller_name=row.get("reseller_name"),
                all_workspaces=row["all_workspaces"],
            )
            for row in rows or []
        ]
    except Exception as e:
        logger.error(f"Error fetching umbrella grants for {user_id}: {e}")
        raise


async def get_user_workspace_access(user_id: str) -> List[WorkspaceAccess]:
    """List every workspace a user can reach, explicit rows winning."""
    query, values = get_user_workspace_access_query(user_id)
    try:
        rows = await run_parameterized_query(query, values)
        return [
            WorkspaceAccess(
                merchant_id=row["merchant_id"],
                name=row.get("name"),
                source=row["source"],
                via_reseller=row.get("via_reseller"),
            )
            for row in rows or []
        ]
    except Exception as e:
        logger.error(f"Error fetching workspace access for {user_id}: {e}")
        raise
