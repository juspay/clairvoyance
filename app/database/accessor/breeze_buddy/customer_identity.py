"""Accessor functions for customer_identity table."""

from typing import Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.customer_identity import (
    decode_customer_identity,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.customer_identity import (
    get_customer_id_for_phone_query,
    upsert_alias_query,
)
from app.schemas.breeze_buddy.memory import CustomerIdentity


async def upsert_alias(
    reseller_id: str,
    merchant_id: str,
    phone: str,
    customer_id: str,
) -> Optional[CustomerIdentity]:
    try:
        q, v = upsert_alias_query(reseller_id, merchant_id, phone, customer_id)
        rows = await run_parameterized_query(q, v)
        if rows:
            return decode_customer_identity(rows[0])
        return None
    except Exception as e:
        logger.error(
            f"[customer_identity] upsert_alias failed "
            f"(phone={phone!r}, customer_id={customer_id!r}): {e}",
            exc_info=True,
        )
        raise


async def get_customer_id_for_phone(
    reseller_id: str,
    merchant_id: str,
    phone: str,
) -> Optional[str]:
    try:
        q, v = get_customer_id_for_phone_query(reseller_id, merchant_id, phone)
        rows = await run_parameterized_query(q, v)
        if rows:
            identity = decode_customer_identity(rows[0])
            return identity.customer_id if identity else None
        return None
    except Exception as e:
        logger.error(
            f"[customer_identity] get_customer_id_for_phone failed "
            f"(phone={phone!r}): {e}",
            exc_info=True,
        )
        raise
