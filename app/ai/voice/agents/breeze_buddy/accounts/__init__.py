"""Provider accounts — the account a template's LLM / STT / TTS runs on.

A credentials-table row may hold a PROVIDER ACCOUNT: its ``provider`` names
the service (``elevenlabs``, ``azure_openai``, ...) and its value carries what
that service reads (a key, a key and its endpoint, a service account). A
template block names one by ``credential_id``; ``Accounts``, built once per
call from the call's tenant, resolves every block to a typed account that
carries its own host — the row's when the block names one, else the
environment's. The vocabulary lives in code, never in a CHECK.

This package is the door: every other module imports from here, never from
the files inside. ``types.py`` = the shapes; ``rows.py`` = who may use a
row; ``blocks.py`` = the template side of the vocabulary; ``resolve.py`` =
the resolver and the environment's accounts.

Phases (docs/PROVIDER_CREDENTIALS.md): (1) rows carry a provider; (2) a
template block names an account, checked at save — this; (3) the LLM runs
on it; (4) speech.
"""

from app.ai.voice.agents.breeze_buddy.accounts.blocks import (
    AccountRefused,
    account_blocks,
    kind_of,
    unwrap_dragontts,
    vendor_of,
)
from app.ai.voice.agents.breeze_buddy.accounts.resolve import (
    Accounts,
    accounts_for_template,
    elevenlabs_host,
    env_account,
)
from app.ai.voice.agents.breeze_buddy.accounts.rows import in_tenant, refusal
from app.ai.voice.agents.breeze_buddy.accounts.types import (
    SHAPES,
    Account,
    AccountShapeError,
    AzureAccount,
    BedrockAccount,
    GcpAccount,
    KeyAccount,
    KeyOnlyAccount,
    VertexAccount,
    account_from_value,
    shape_problems,
)

__all__ = [
    "SHAPES",
    "Account",
    "AccountRefused",
    "AccountShapeError",
    "Accounts",
    "AzureAccount",
    "BedrockAccount",
    "GcpAccount",
    "KeyAccount",
    "KeyOnlyAccount",
    "VertexAccount",
    "account_blocks",
    "account_from_value",
    "accounts_for_template",
    "elevenlabs_host",
    "env_account",
    "in_tenant",
    "kind_of",
    "refusal",
    "shape_problems",
    "unwrap_dragontts",
    "vendor_of",
]
