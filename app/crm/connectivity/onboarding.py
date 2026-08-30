"""WhatsApp onboarding — owned by the connectivity module.

BUSINESS LOGIC ONLY — DB mechanics live in db/accessor.py, Graph API calls
in meta_graph.py. GATHER (exchange the Embedded Signup code for a
long-lived token, verify the phone number) -> the credential upsert (an
external system, not a crm_* table, so it sits outside the atomic write) ->
APPLY (one transaction for the installation + its primary binding).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.core.logger.context import update_log_context
from app.crm.connectivity import meta_graph as whatsapp
from app.crm.connectivity.db import DbTxn, accessor, atomically
from app.crm.connectivity.schemas import InstallationRead
from app.database.accessor.breeze_buddy.credentials import (
    create_credential,
    get_credential_by_name,
    update_credential,
)
from app.database.accessor.breeze_buddy.merchants import get_merchants_by_ids
from app.schemas import CredentialType

_NO_RECEIVER_WHY = (
    "no inbound webhook receiver exists in this repo yet — delivery "
    "receipts and inbound replies are not processed"
)

CONNECTOR_KEY = "whatsapp"
CHANNEL = "whatsapp"


class OnboardingError(Exception):
    """Merchant lookup or credential persistence failed before any
    connectivity row was written."""


def _credential_name(merchant_id: str, waba_id: str) -> str:
    """credentials has no merchant column (only reseller_id) — the name
    itself must encode the merchant + account or two merchants under one
    reseller collide on one row."""
    return f"whatsapp:{merchant_id}:{waba_id}"


async def _find_credential(reseller_id: Optional[str], name: str):
    return await get_credential_by_name(reseller_id, name, mask=False)


async def onboard_whatsapp(
    merchant_id: str,
    code: str,
    waba_id: str,
    phone_number_id: str,
    display_label: Optional[str] = None,
) -> InstallationRead:
    """Complete Meta's Embedded Signup flow and write the installation +
    primary binding. Idempotent: re-onboarding the same WABA/number updates
    the existing rows in place (the schema's own unique indexes enforce
    this) rather than duplicating."""
    update_log_context(waba_id=waba_id, phone_number_id=phone_number_id)

    merchants, _ = await get_merchants_by_ids([merchant_id])
    if not merchants:
        raise OnboardingError(f"unknown merchant_id: {merchant_id}")
    reseller_id = merchants[0].reseller_id

    logger.info("onboard_whatsapp: exchanging Embedded Signup code for a token")
    short_lived_token = await whatsapp.exchange_code_for_token(code)
    long_lived_token = await whatsapp.exchange_for_long_lived_token(short_lived_token)
    logger.info("onboard_whatsapp: verifying phone number against WABA")
    await whatsapp.verify_phone_number(waba_id, phone_number_id, long_lived_token)

    logger.info("onboard_whatsapp: subscribing WABA to webhooks")
    try:
        await whatsapp.subscribe_to_webhooks(waba_id, long_lived_token)
        health_level, health_why = "subscribed", _NO_RECEIVER_WHY
    except whatsapp.WhatsappProviderError as e:
        logger.warning(f"onboard_whatsapp: webhook subscription failed: {e}")
        health_level = "authenticated"
        health_why = f"webhook subscription failed: {e}"
    health_detail = {
        "level": health_level,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "why": health_why,
    }

    credential_name = _credential_name(merchant_id, waba_id)
    credential_value = {whatsapp.TOKEN_KEY: long_lived_token}
    description = (
        f"Meta WhatsApp long-lived token — merchant {merchant_id}, WABA {waba_id}"
    )

    existing_credential = await _find_credential(reseller_id, credential_name)
    if existing_credential:
        logger.info("onboard_whatsapp: rotating existing WhatsApp credential")
        credential = await update_credential(
            existing_credential.id,
            credential_type=CredentialType.CUSTOM,
            value=credential_value,
        )
    else:
        logger.info("onboard_whatsapp: creating new WhatsApp credential")
        credential = await create_credential(
            reseller_id,
            credential_name,
            CredentialType.CUSTOM,
            credential_value,
            description=description,
        )
    if credential is None:
        raise OnboardingError("failed to persist WhatsApp credential")

    installation = await atomically(
        _onboard_in_txn,
        merchant_id,
        waba_id,
        phone_number_id,
        credential.id,
        display_label,
        health_detail,
    )
    logger.info(
        f"onboard_whatsapp: installation {installation.id} healthy, "
        f"status={installation.status}"
    )
    return installation


async def _onboard_in_txn(
    txn: DbTxn,
    merchant_id: str,
    waba_id: str,
    phone_number_id: str,
    credential_id: str,
    display_label: Optional[str],
    health_detail: Dict[str, Any],
) -> InstallationRead:
    """ATOMIC: installation + its primary binding — a half-onboarded
    merchant (an installation with no usable binding, or a binding hanging
    off no installation) must never be visible."""
    installation = await accessor.upsert_installation(
        txn,
        merchant_id,
        CONNECTOR_KEY,
        waba_id,
        display_label,
        credential_id,
        "healthy",
        health_detail,
    )
    existing_binding = await accessor.get_channel_binding_by_address(
        txn, merchant_id, CHANNEL, phone_number_id
    )
    if existing_binding is not None and existing_binding.status == "retired":
        raise OnboardingError(
            f"phone number {phone_number_id} was retired — cannot re-onboard "
            "the same binding"
        )
    already_has_primary = await accessor.has_primary_binding(txn, merchant_id, CHANNEL)
    await accessor.upsert_channel_binding(
        txn,
        merchant_id,
        CHANNEL,
        installation.id,
        phone_number_id,
        not already_has_primary,
    )
    return installation


async def list_installations(merchant_id: str) -> List[InstallationRead]:
    return await accessor.list_installations(merchant_id)


async def get_installation(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    return await accessor.get_installation_read(merchant_id, installation_id)


async def disconnect(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """Status -> revoked. Never deletes (the schema's own law — history is
    the point of this table). Returns None if installation_id doesn't
    belong to merchant_id (fail closed, CRM law #6)."""
    return await atomically(_disconnect_in_txn, merchant_id, installation_id)


async def _disconnect_in_txn(
    txn: DbTxn, merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """ATOMIC: revoking the installation and pausing its bindings share one
    fate — a revoked installation must never leave a binding that still
    claims to be an active send route."""
    installation = await accessor.disconnect_installation(
        txn, merchant_id, installation_id
    )
    if installation is None:
        return None
    await accessor.pause_bindings_for_installation(txn, merchant_id, installation_id)
    return installation
