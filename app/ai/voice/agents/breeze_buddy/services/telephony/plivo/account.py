"""The Plivo account a call ran on, for the recording download and the MPC
callback, which start from the lead: its template's account."""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.accounts import (
    Accounts,
    PlivoAccount,
    template_plivo_account,
)
from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.schemas import LeadCallTracker


async def lead_plivo_account(lead: LeadCallTracker) -> PlivoAccount:
    """The account the lead's template names, else the environment's.
    Raises AccountRefused."""
    template = (
        await get_template_by_id_cached(lead.template_id) if lead.template_id else None
    )
    return await template_plivo_account(
        Accounts(lead.reseller_id, lead.merchant_id),
        getattr(template, "configurations", None),
    )
