"""Load Juspay credentials from the credentials table."""

from dataclasses import dataclass
from typing import Optional

from app.core.config.static import JUSPAY_BASE_URL
from app.database.accessor.breeze_buddy.credentials import get_credentials_by_merchant
from app.services.uap.client import JuspayError

# One `custom` credential row per reseller holding every Juspay field, so the
# key and the environment it belongs to can never be fetched out of sync:
#   {"api_key": "...", "merchant_id": "...", "base_url": "...", "partner_id": "..."}
CREDENTIAL_NAME = "uap"


@dataclass(frozen=True)
class JuspayCredentials:
    api_key: str
    # Juspay's merchant id — NOT our reseller id.
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

    # Ordered reseller_id NULLS FIRST, so the LAST match is the reseller's
    # own override of a global row. is_active is re-checked rather than
    # trusted from the query's WHERE — a deactivated key must never be used.
    match = None
    for row in rows:
        if row.name == name and row.is_active:
            match = row
    if match is None:
        raise JuspayError(f"No active '{name}' credential for {reseller_id}", None, None)

    value = match.value or {}
    api_key = value.get("api_key")
    merchant_id = value.get("merchant_id")
    if not api_key or not merchant_id:
        # Also lands here when KMS decryption failed — the decoder returns
        # None for an undecryptable value.
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
