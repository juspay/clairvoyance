"""The telephony side: which vendor a telephony block names, the
environment's account for it, and the host rule a row must obey on this
service. A template's ``telephony_configuration`` names the Plivo account
its calls run on, the way ``stt_configuration`` names its STT account."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.accounts.types import (
    Account,
    AccountRefused,
    PlivoAccount,
)
from app.ai.voice.agents.breeze_buddy.template.types import TelephonyConfiguration
from app.core.config import static

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.accounts.resolve import Accounts

TELEPHONY_VENDOR: Dict[str, str] = {"plivo": "plivo"}


def plivo_keys(account: Optional[PlivoAccount]) -> Tuple[str, str]:
    """The (auth_id, auth_token) a Plivo REST call is signed with: the
    account's, else the environment's — the only place a call reads the
    environment's Plivo keys."""
    if account is not None:
        return account.auth_id, account.auth_token
    return str(static.PLIVO_AUTH_ID or ""), str(static.PLIVO_AUTH_TOKEN or "")


async def env_account(vendor: str, block: Any) -> Account:
    """The environment's telephony account for a vendor. Raises
    AccountRefused, and only AccountRefused, when the environment has none."""
    if vendor == "plivo":
        auth_id, auth_token = plivo_keys(None)
        if not auth_id or not auth_token:
            raise AccountRefused(
                "PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN are required for plivo"
            )
        try:
            return PlivoAccount(auth_id=auth_id, auth_token=auth_token)
        except ValidationError as e:
            # Field and message only — never the value (a key) itself.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors()
            )
            raise AccountRefused(f"the environment's Plivo keys: {problems}") from e
    raise AccountRefused(f"no provider account exists for {vendor.split(':', 1)[-1]}")


def row_account(vendor: str, account: Account, credential_id: str) -> Account:
    """Plivo has one API host: a row's account serves as it is."""
    return account


async def template_plivo_account(
    accounts: "Accounts", configurations: Any
) -> PlivoAccount:
    """The Plivo account a template's calls run on — the row its
    ``telephony_configuration`` names, else the environment's, asked of the
    resolver like every STT and TTS block. Raises AccountRefused."""
    block = (
        getattr(configurations, "telephony_configuration", None)
        or TelephonyConfiguration()
    )
    return await accounts.get(block, PlivoAccount)
