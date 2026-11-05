"""account.update — a notice about the WABA itself.

Merchant-level (about="merchant"), like the template letters. The values
arrive flat, so the fields are plain payload.* paths — except the ban
state, which Meta ships as a one-element list inside ban_info, the one
derive() this concern needs.
"""

from typing import Any, Dict, List, Optional

from app.crm.record.extractors.whatsapp.shared import _f
from app.crm.record.schemas import CatalogField


def ban_state(payload: Dict[str, Any]) -> Optional[Any]:
    """The WABA's ban state on an account notice — Meta ships it as a
    one-element list inside ban_info; absent when the letter is no ban."""
    ban = payload.get("ban_info")
    if not isinstance(ban, dict):
        return None
    state = ban.get("waba_ban_state")
    if isinstance(state, list):
        return state[0] if state else None
    return state


def fields() -> List[CatalogField]:
    """A notice about the WABA itself — verification, restriction, a ban."""
    return [
        _f("payload.event", "text", "Account event", variable=True),
        _f("ban_state", "text", "Ban state", variable=True, derived=True),
        _f("payload.ban_info.waba_ban_date", "text", "Ban date", variable=True),
    ]
