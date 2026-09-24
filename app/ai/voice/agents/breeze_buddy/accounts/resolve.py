"""One resolver per call: which account does this block run on?

``Accounts``, built from the CALL's tenant, answers every factory's question
from the row when the block names one (``credential_id``) and from the
environment when it does not. Each service owns its words, its environment
accounts and the host rule a row must obey on it — ``llm.py`` (text and
realtime), ``stt.py``, ``tts.py``; this file only dispatches by the block's
kind. The factories read the key and the host from a typed account and
never touch an environment variable themselves, so a row can never be
right for one service and wrong for another, and a key never travels apart
from its host.

Fail closed: a row is used only when it exists, is active, names the SAME
provider the block names, sits in the caller's tenant (its merchant, its
reseller, or global), carries that provider's fields, and can serve the
service asking. Anything else raises — a call on the wrong account, or on
another tenant's account, is never placed. ``Accounts.problems`` asks the
same questions at template save, so a bad reference is a 422, not a dead
call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, overload

from app.ai.voice.agents.breeze_buddy.accounts import llm, stt, tts
from app.ai.voice.agents.breeze_buddy.accounts.blocks import (
    account_blocks,
    kind_of,
    vendor_of,
)
from app.ai.voice.agents.breeze_buddy.accounts.rows import refusal
from app.ai.voice.agents.breeze_buddy.accounts.types import (
    SHAPES,
    Account,
    AccountRefused,
    account_from_value,
)
from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id

T = TypeVar("T", bound=Account)


async def env_account(vendor: str, kind: str, block: Any) -> Account:
    """The environment's account for a block — the ONLY place an env key is
    read for a call; each service answers for its own vendors."""
    if kind == "stt":
        return await stt.env_account(vendor, block)
    if kind == "tts":
        return await tts.env_account(vendor, block)
    return await llm.env_account(vendor, kind, block)


def _row_account(
    vendor: str, kind: str, account: Account, credential_id: str
) -> Account:
    """The host rule a row must obey on the service asking."""
    if kind == "stt":
        return stt.row_account(vendor, account, credential_id)
    if kind == "tts":
        return tts.row_account(vendor, account, credential_id)
    return llm.row_account(vendor, account, credential_id)


@dataclass
class Accounts:
    """The provider accounts one call runs on. Built at the entry point from
    the CALL's tenant (the lead's, kept across transfers; the template's for
    chat, the greeting and IVR) and passed down; each block is resolved
    lazily, once, so a chat turn reads at most the LLM row."""

    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    _memo: Dict[Tuple[Any, ...], Account] = field(default_factory=dict)

    @overload
    async def get(self, block: Any) -> Account: ...

    @overload
    async def get(self, block: Any, expect: Type[T]) -> T: ...

    async def get(self, block: Any, expect: Optional[Type[Any]] = None) -> Any:
        """The account this block runs on — its row when it names one, else
        the environment's. With ``expect``, the shape the caller can use:
        any other shape is refused (AccountRefused, never an assertion — a
        factory reads the fields of the shape it asked for). Raises
        AccountRefused."""
        account = await self._get(block)
        if expect is not None and not isinstance(account, expect):
            raise AccountRefused(
                f"{vendor_of(block)} resolved to {type(account).__name__}, "
                f"expected {expect.__name__}"
            )
        return account

    async def _get(self, block: Any) -> Account:
        if isinstance(block, TTSConfig):
            block = tts.unwrap_dragontts(block)
        kind = kind_of(block)
        vendor = vendor_of(block)
        credential_id = getattr(block, "credential_id", None) or None
        key = (
            vendor,
            kind,
            credential_id,
            getattr(block, "endpoint", None),
            getattr(block, "api_key_name", None),
        )
        if key not in self._memo:
            if vendor not in SHAPES:
                raise AccountRefused(
                    f"no provider account exists for {vendor.split(':', 1)[-1]}"
                )
            if credential_id:
                self._memo[key] = await self._row(vendor, kind, credential_id)
            else:
                self._memo[key] = await env_account(vendor, kind, block)
        return self._memo[key]

    async def _row(self, vendor: str, kind: str, credential_id: str) -> Account:
        credential = await get_credential_by_id(
            credential_id, mask=False, raise_errors=True
        )
        reason = refusal(
            credential, credential_id, vendor, self.reseller_id, self.merchant_id
        )
        if reason:
            raise AccountRefused(reason)
        assert credential is not None
        try:
            account: Account = account_from_value(vendor, credential.value)
        except ValueError as e:
            raise AccountRefused(
                f"credential {credential_id} is incomplete for {vendor}: {e}"
            ) from e
        account = _row_account(vendor, kind, account, credential_id)
        logger.info(
            f"provider account resolved: {vendor} credential={credential_id} "
            f"(reseller={self.reseller_id} merchant={self.merchant_id})"
        )
        return account

    async def problems(self, configurations: Any) -> List[str]:
        """Save-time check: the same path a call takes, for every block that
        names an account. Empty = clean."""
        found: List[str] = []
        for name, block in account_blocks(configurations):
            if not getattr(block, "credential_id", None):
                continue
            try:
                await self.get(block)
            except ValueError as e:  # AccountRefused and pydantic's both subclass it
                found.append(f"{name}: {e}")
        return found


def accounts_for_template(template: Any) -> Accounts:
    """An ``Accounts`` in the template's own tenant — for the paths that
    have no lead: chat turns, the greeting job, template save."""
    return Accounts(
        reseller_id=getattr(template, "reseller_id", None),
        merchant_id=getattr(template, "merchant_id", None),
    )
