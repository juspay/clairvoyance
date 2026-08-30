"""Async accessor functions for ui_component (migration 057).

Thin wrappers around ``run_parameterized_query`` + ``decode_ui_component``.
Returns Pydantic models, never raw rows. Errors are logged + re-raised so
route handlers can convert them into the appropriate HTTP status.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.ui_component import decode_ui_component
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.ui_component import (
    create_ui_component_query,
    delete_ui_component_query,
    get_ui_component_by_id_query,
    get_ui_components_by_names_query,
    list_ui_components_query,
    update_ui_component_query,
)
from app.schemas.breeze_buddy.ui_component import UiComponentResponse


async def create_ui_component(
    *,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    props_schema: Dict[str, Any],
    flags: Dict[str, Any],
    render_def: Optional[Dict[str, Any]],
    prompt_hint: Optional[str],
    is_active: bool = True,
) -> Optional[UiComponentResponse]:
    query, values = create_ui_component_query(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        name=name,
        props_schema=json.dumps(props_schema),
        flags=json.dumps(flags),
        render_def=json.dumps(render_def) if render_def is not None else None,
        prompt_hint=prompt_hint,
        is_active=is_active,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            logger.info(
                f"Created ui_component: reseller={reseller_id} "
                f"merchant={merchant_id} name={name} id={row['id']}"
            )
            return decode_ui_component(row)
        return None
    except Exception as e:
        logger.error(
            f"Error creating ui_component {name!r} for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        raise


async def get_ui_component_by_id(
    ui_component_id: str,
) -> Optional[UiComponentResponse]:
    query, values = get_ui_component_by_id_query(ui_component_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_ui_component(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching ui_component {ui_component_id}: {e}")
        raise


async def get_ui_components_by_names(
    *,
    reseller_id: str,
    merchant_id: Optional[str],
    names: List[str],
) -> List[UiComponentResponse]:
    """Session-start def fetch: active rows for the template's opt-in names
    (merchant-specific rows shadow reseller-wide ones). Missing names are
    simply absent from the result — the caller decides whether that's an
    error or a silently-narrower catalog."""
    if not names:
        return []
    query, values = get_ui_components_by_names_query(
        reseller_id=reseller_id, merchant_id=merchant_id, names=names
    )
    try:
        rows = await run_parameterized_query(query, values)
        return [decode_ui_component(r) for r in rows] if rows else []
    except Exception as e:
        logger.error(
            f"Error fetching ui_components {names} for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        raise


async def list_ui_components(
    *,
    page: int = 1,
    limit: int = 50,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> Tuple[List[UiComponentResponse], int]:
    query, count_query, values = list_ui_components_query(
        page=page,
        limit=limit,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=reseller_ids,
        include_inactive=include_inactive,
    )
    try:
        rows = await run_parameterized_query(query, values)
        count_result = await run_parameterized_query(count_query, values[:-2])
        count_row = count_result[0] if count_result else None
        items = [decode_ui_component(r) for r in rows] if rows else []
        total = count_row["total"] if count_row else 0
        return items, total
    except Exception as e:
        logger.error(f"Error listing ui_components: {e}")
        raise


async def update_ui_component(
    ui_component_id: str,
    *,
    props_schema: Optional[Dict[str, Any]] = None,
    flags: Optional[Dict[str, Any]] = None,
    render_def: Optional[Dict[str, Any]] = None,
    prompt_hint: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[UiComponentResponse]:
    query, values = update_ui_component_query(
        ui_component_id=ui_component_id,
        props_schema=json.dumps(props_schema) if props_schema is not None else None,
        flags=json.dumps(flags) if flags is not None else None,
        render_def=json.dumps(render_def) if render_def is not None else None,
        prompt_hint=prompt_hint,
        is_active=is_active,
    )
    if not values:
        return await get_ui_component_by_id(ui_component_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            logger.info(f"Updated ui_component: {ui_component_id}")
            return decode_ui_component(row)
        return None
    except Exception as e:
        logger.error(f"Error updating ui_component {ui_component_id}: {e}")
        raise


async def delete_ui_component(ui_component_id: str) -> bool:
    query, values = delete_ui_component_query(ui_component_id)
    try:
        result = await run_parameterized_query(query, values)
        deleted = bool(result)
        if deleted:
            logger.info(f"Deleted ui_component: {ui_component_id}")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting ui_component {ui_component_id}: {e}")
        raise
