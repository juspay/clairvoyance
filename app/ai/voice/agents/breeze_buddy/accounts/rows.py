"""The rules on a credential ROW: who may use it."""

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
