"""Customer identity resolution for memory keying.

Resolution chain (per §2.1 of the spec):
  1. customer_id in payload/template_vars -> (customer_id, "customer_id")
  2. phone present -> look up customer_identity alias -> (customer_id, "customer_id")
  3. phone present, no alias -> ("phone:<normalized>", "phone")  [provisional]
  4. neither -> None  (memory off for this conversation)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger
from app.database.accessor.breeze_buddy.customer_identity import (
    get_customer_id_for_phone,
)
from app.database.queries.breeze_buddy.blacklisted_numbers import normalize_phone_number


async def resolve_customer_key(
    reseller_id: str,
    merchant_id: str,
    payload: Optional[Dict[str, Any]],
    id_field: str = "customer_id",
    phone_field: str = "customer_mobile_number",
    allow_phone_key: bool = True,
) -> Optional[Tuple[str, str]]:
    """Return (customer_key, key_type) or None if identity cannot be determined.

    Performs a DB lookup for step 2 (phone alias), so this is async.
    Best-effort: any error returns None to keep memory opt-in safe.
    """
    if not payload:
        return None

    try:
        # Step 1: direct customer_id
        customer_id = payload.get(id_field) or payload.get("customer_id")
        if customer_id and str(customer_id).strip():
            return (str(customer_id).strip(), "customer_id")

        # Step 2 & 3: phone path
        phone_raw = payload.get(phone_field) or payload.get("customer_mobile_number")
        if not phone_raw:
            return None

        normalized = normalize_phone_number(str(phone_raw))
        if not normalized:
            return None

        # Step 2: alias lookup
        try:
            mapped_id = await get_customer_id_for_phone(
                reseller_id, merchant_id, normalized
            )
            if mapped_id:
                return (mapped_id, "customer_id")
        except Exception as alias_err:
            logger.warning(
                f"[memory.identity] alias lookup failed for phone {normalized!r}: "
                f"{alias_err}"
            )

        # Step 3: provisional phone key
        if allow_phone_key:
            return (f"phone:{normalized}", "phone")

        return None

    except Exception as e:
        logger.error(
            f"[memory.identity] resolve_customer_key error: {e}", exc_info=True
        )
        return None
