"""The rules on a credential ROW: who may use it, and whether it may serve
the block that names it."""

from __future__ import annotations

from typing import Optional

from app.schemas import Credential


def in_tenant(
    credential: Credential, reseller_id: Optional[str], merchant_id: Optional[str]
) -> bool:
    """PURE: may (reseller, merchant) use this row? Global rows (no
    reseller) may be used by anyone; a reseller row by that reseller; a
    merchant row only by that merchant. Compared as text."""
    if credential.reseller_id is None:
        return True
    if str(credential.reseller_id) != str(reseller_id or ""):
        return False
    if credential.merchant_id is None:
        return True
    return str(credential.merchant_id) == str(merchant_id or "")


def refusal(
    credential: Optional[Credential],
    credential_id: str,
    vendor: str,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
) -> Optional[str]:
    """PURE: the one reason this row may not serve, or None."""
    if credential is None:
        return f"credential {credential_id} does not exist"
    if not credential.is_active:
        return f"credential {credential_id} is inactive"
    if not in_tenant(credential, reseller_id, merchant_id):
        return f"credential {credential_id} belongs to another tenant"
    if credential.provider != vendor:
        return (
            f"credential {credential_id} is for {credential.provider or 'no'} "
            f"provider, this block needs {vendor}"
        )
    return None
