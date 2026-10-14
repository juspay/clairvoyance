"""The campaign registry (canon T22, canon/06-outreach.md) — the badge a
broadcast wears. It names a season and never contains it: nothing points
from a campaign to anything, and a campaign never blocks a send. The
composite FK from crm_broadcast (088) is why two spellings of Diwali can
never split one season's report.
"""

import re
from datetime import date
from typing import List, Optional

from app.crm.outreach.db import UniqueViolation
from app.crm.outreach.db.accessors import campaign as campaign_accessor
from app.crm.outreach.schemas import Campaign

#: The key's format — 087's CHECK, spelled here too so a bad key is a
#: readable refusal before it is an insert error.
CAMPAIGN_KEY_FORMAT = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_CAMPAIGN_KEY = 64

#: A merchant has a handful of seasons; the list is one page.
MAX_CAMPAIGNS_LISTED = 500


class CampaignKeyTaken(Exception):
    """The merchant already has a campaign with this key."""


def display_name(raw: str, what: str) -> str:
    """A name as stored: surrounding whitespace dropped (the 087 / 088 CHECKs
    refuse an untrimmed one). Pure. Raises ValueError when nothing is left."""
    name = (raw or "").strip()
    if not name:
        raise ValueError(f"a {what} needs a name")
    return name


def campaign_key(raw: str) -> str:
    """A key as stored: trimmed and lowercased ("Diwali-2026" and
    "diwali-2026" are one season), then held to the slug format. Pure.
    Raises ValueError."""
    key = (raw or "").strip().lower()
    if len(key) > MAX_CAMPAIGN_KEY or not CAMPAIGN_KEY_FORMAT.match(key):
        raise ValueError(
            "a campaign key is lowercase letters and digits joined by single "
            f"hyphens (diwali-2026), at most {MAX_CAMPAIGN_KEY} characters"
        )
    return key


async def create_campaign(
    merchant_id: str,
    key: str,
    name: str,
    starts_at: Optional[date],
    ends_at: Optional[date],
    created_by: Optional[str],
) -> Campaign:
    """A new badge. Raises ValueError (bad key, blank name, a window that
    ends before it starts) or CampaignKeyTaken."""
    stored_key = campaign_key(key)
    stored_name = display_name(name, "campaign")
    if starts_at and ends_at and ends_at < starts_at:
        raise ValueError("ends_at is before starts_at")
    try:
        return await campaign_accessor.insert_campaign(
            merchant_id, stored_key, stored_name, starts_at, ends_at, created_by
        )
    except UniqueViolation as e:
        raise CampaignKeyTaken(stored_key) from e


async def list_campaigns(merchant_id: str) -> List[Campaign]:
    """The merchant's campaigns, newest first."""
    return await campaign_accessor.list_campaigns(merchant_id, MAX_CAMPAIGNS_LISTED)
