"""Tenant-safe customer identity resolution for persistent memory."""

from __future__ import annotations

from typing import Any, Dict, Optional

import phonenumbers

from app.core.logger import logger
from app.database.accessor.breeze_buddy.customer_identity import (
    get_alias_for_phone,
)
from app.schemas.breeze_buddy.memory import MemoryIdentity


def normalize_memory_phone_number(
    phone_number: str, default_region: Optional[str] = None
) -> str:
    """Return validated E.164 or ``""`` for invalid/ambiguous input."""
    raw = (phone_number or "").strip()
    if not raw:
        return ""
    region = None if raw.startswith("+") else (default_region or "").upper() or None
    if not raw.startswith("+") and region is None:
        return ""
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(parsed):
        return ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


async def resolve_memory_identity(
    reseller_id: str,
    merchant_id: str,
    payload: Optional[Dict[str, Any]],
    *,
    id_field: str = "customer_id",
    phone_field: str = "customer_mobile_number",
    phone_default_region: Optional[str] = None,
    allow_phone_key: bool = True,
) -> Optional[MemoryIdentity]:
    """Resolve a canonical customer scope while retaining observed aliases.

    Alias-store failures fail closed: creating a new provisional identity while
    the alias table is unavailable can split one customer across two memories.
    """
    if not reseller_id.strip() or not merchant_id.strip() or not payload:
        return None

    explicit_raw = payload.get(id_field) or payload.get("customer_id")
    explicit_customer_id = str(explicit_raw).strip() if explicit_raw is not None else ""

    phone_raw = payload.get(phone_field) or payload.get("customer_mobile_number")
    phone = (
        normalize_memory_phone_number(str(phone_raw), phone_default_region)
        if phone_raw
        else ""
    )

    if explicit_customer_id:
        return MemoryIdentity(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            customer_key=explicit_customer_id,
            key_type="customer_id",
            phone=phone or None,
            explicit_customer_id=explicit_customer_id,
        )

    if not phone:
        return None

    try:
        alias = await get_alias_for_phone(reseller_id, merchant_id, phone)
    except Exception as error:
        logger.warning(
            "[memory.identity] alias lookup failed; memory disabled "
            f"(scope={_tenant_digest(reseller_id, merchant_id)}): "
            f"{type(error).__name__}"
        )
        return None

    if alias is not None:
        if alias.status != "ACTIVE":
            logger.warning(
                "[memory.identity] conflicted alias; phone fallback disabled "
                f"(scope={_tenant_digest(reseller_id, merchant_id)})"
            )
            return None
        return MemoryIdentity(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            customer_key=alias.customer_id,
            key_type="customer_id",
            phone=phone,
        )

    if not allow_phone_key:
        return None
    return MemoryIdentity(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        customer_key=f"phone:{phone}",
        key_type="phone",
        phone=phone,
    )


async def resolve_customer_key(
    reseller_id: str,
    merchant_id: str,
    payload: Optional[Dict[str, Any]],
    id_field: str = "customer_id",
    phone_field: str = "customer_mobile_number",
    allow_phone_key: bool = True,
    phone_default_region: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Compatibility wrapper for callers that only need the selected key."""
    identity = await resolve_memory_identity(
        reseller_id,
        merchant_id,
        payload,
        id_field=id_field,
        phone_field=phone_field,
        phone_default_region=phone_default_region,
        allow_phone_key=allow_phone_key,
    )
    return (identity.customer_key, identity.key_type) if identity else None


def _tenant_digest(reseller_id: str, merchant_id: str) -> str:
    identity = MemoryIdentity(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        customer_key="_redacted",
        key_type="customer_id",
    )
    return identity.scope_digest[:12]
