"""Load Juspay credentials from the credentials table."""

from dataclasses import dataclass
from typing import Optional

from app.core.config.static import JUSPAY_BASE_URL
from app.database.accessor.breeze_buddy.credentials import get_credentials_by_merchant
from app.services.uap.client import JuspayError

CREDENTIAL_NAME = "uap"


@dataclass(frozen=True)
class JuspayCredentials:
    api_key: str
    merchant_id: str
    base_url: str
    partner_id: Optional[str] = None


async def load_uap_credentials(
    reseller_id: str, name: str = CREDENTIAL_NAME
) -> JuspayCredentials:
    """Load a reseller's UAP credential, falling back to the global row.

    The lookup returns every credential this reseller can see (its own plus
    all global ones), so ``name`` is what selects ours out of the set.
    """
    rows = await get_credentials_by_merchant(reseller_id, mask=False)

    match = None
    for row in rows:
        if row.name == name and row.is_active:
            match = row
    if match is None:
        raise JuspayError(
            f"No active '{name}' credential for {reseller_id}", None, None
        )

    value = match.value or {}
    api_key = value.get("api_key")
    merchant_id = value.get("merchant_id")
    if not api_key or not merchant_id:
        raise JuspayError(
            f"'{name}' credential unusable: missing api_key or merchant_id "
            f"(or failed to decrypt)",
            None,
            None,
        )

    return JuspayCredentials(
        api_key=api_key,
        merchant_id=merchant_id,
        base_url=value.get("base_url") or JUSPAY_BASE_URL,
        partner_id=value.get("partner_id"),
    )
