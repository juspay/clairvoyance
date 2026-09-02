"""Async accessor functions for widget_config (migration 030).

Thin wrappers around ``run_parameterized_query`` + ``decode_widget_config``.
Returns Pydantic models, never raw rows. Errors are logged + re-raised
so route handlers can convert them into the appropriate HTTP status.
"""

from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.widget_config import decode_widget_config
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.widget_config import (
    create_widget_config_query,
    delete_widget_config_query,
    get_widget_config_by_id_query,
    get_widget_config_by_public_key_query,
    get_widget_config_by_reseller_merchant_query,
    list_widget_configs_query,
    update_widget_config_query,
)
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse


async def create_widget_config(
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
    appearance: Optional[Dict[str, Any]] = None,
) -> Optional[WidgetConfigResponse]:
    query, values = create_widget_config_query(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        public_widget_key=public_widget_key,
        template_id=template_id,
        allowed_origins=allowed_origins,
        max_sessions_per_ip_hour=max_sessions_per_ip_hour,
        max_messages_per_ip_hour=max_messages_per_ip_hour,
        max_concurrent_per_ip=max_concurrent_per_ip,
        max_voice_sessions_per_ip_hour=max_voice_sessions_per_ip_hour,
        active=active,
        appearance=appearance,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            logger.info(
                f"Created widget_config: reseller={reseller_id} merchant={merchant_id} "
                f"id={row['id']}"
            )
            return decode_widget_config(row)
        return None
    except Exception as e:
        logger.error(
            f"Error creating widget_config for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        raise


async def get_widget_config_by_id(
    widget_config_id: str,
) -> Optional[WidgetConfigResponse]:
    query, values = get_widget_config_by_id_query(widget_config_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_widget_config(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching widget_config {widget_config_id}: {e}")
        raise


async def get_widget_config_by_public_key(
    public_widget_key: str,
) -> Optional[WidgetConfigResponse]:
    """Hot path: resolve a widget_config from the public key the embed sent.

    Returns ``None`` on miss so the handler can decide between 404 (key
    unknown / inactive) and an audit log.
    """
    query, values = get_widget_config_by_public_key_query(public_widget_key)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_widget_config(row) if row else None
    except Exception as e:
        # Log without the actual key — it's a credential.
        logger.error(
            f"Error fetching widget_config by public_widget_key "
            f"(key_prefix={public_widget_key[:6]!r}): {e}"
        )
        raise


async def get_widget_config_by_reseller_merchant(
    reseller_id: str, merchant_id: str
) -> Optional[WidgetConfigResponse]:
    query, values = get_widget_config_by_reseller_merchant_query(
        reseller_id, merchant_id
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_widget_config(row) if row else None
    except Exception as e:
        logger.error(
            f"Error fetching widget_config for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        raise


async def list_widget_configs(
    *,
    page: int = 1,
    limit: int = 50,
    reseller_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> Tuple[List[WidgetConfigResponse], int]:
    query, count_query, values = list_widget_configs_query(
        page=page,
        limit=limit,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
        include_inactive=include_inactive,
    )
    try:
        rows = await run_parameterized_query(query, values)
        # values[:-2] strips the LIMIT/OFFSET so the count query sees the
        # same WHERE-bound parameters.
        count_result = await run_parameterized_query(count_query, values[:-2])
        count_row = count_result[0] if count_result else None
        items = [decode_widget_config(r) for r in rows] if rows else []
        total = count_row["total"] if count_row else 0
        return items, total
    except Exception as e:
        logger.error(f"Error listing widget_configs: {e}")
        raise


async def update_widget_config(
    widget_config_id: str,
    *,
    template_id: Optional[str] = None,
    allowed_origins: Optional[List[str]] = None,
    max_sessions_per_ip_hour: Optional[int] = None,
    max_messages_per_ip_hour: Optional[int] = None,
    max_concurrent_per_ip: Optional[int] = None,
    max_voice_sessions_per_ip_hour: Optional[int] = None,
    appearance: Optional[Dict[str, Any]] = None,
    active: Optional[bool] = None,
) -> Optional[WidgetConfigResponse]:
    query, values = update_widget_config_query(
        widget_config_id=widget_config_id,
        template_id=template_id,
        allowed_origins=allowed_origins,
        max_sessions_per_ip_hour=max_sessions_per_ip_hour,
        max_messages_per_ip_hour=max_messages_per_ip_hour,
        max_concurrent_per_ip=max_concurrent_per_ip,
        max_voice_sessions_per_ip_hour=max_voice_sessions_per_ip_hour,
        appearance=appearance,
        active=active,
    )
    if not values:
        # Caller passed no fields — return current row so PUT semantics
        # stay consistent across "no-op" and "real change".
        return await get_widget_config_by_id(widget_config_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row:
            logger.info(f"Updated widget_config: {widget_config_id}")
            return decode_widget_config(row)
        return None
    except Exception as e:
        logger.error(f"Error updating widget_config {widget_config_id}: {e}")
        raise


async def delete_widget_config(widget_config_id: str) -> bool:
    query, values = delete_widget_config_query(widget_config_id)
    try:
        result = await run_parameterized_query(query, values)
        deleted = bool(result)
        if deleted:
            logger.info(f"Deleted widget_config: {widget_config_id}")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting widget_config {widget_config_id}: {e}")
        raise
