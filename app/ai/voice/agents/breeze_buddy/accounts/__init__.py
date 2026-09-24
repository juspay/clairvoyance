"""Provider accounts — the account a template's LLM / STT / TTS runs on.

A credentials-table row may hold a PROVIDER ACCOUNT: its ``provider`` names
the service (``elevenlabs``, ``azure_openai``, ...) and its value carries what
that service reads (a key, a key and its endpoint, a service account). The
vocabulary lives in code, never in a CHECK. This package is the door: every
other module imports from here, never from the files inside.

Phases (docs/PROVIDER_CREDENTIALS.md): (1) rows carry a provider — this;
(2) a template block names an account; (3) the LLM runs on it; (4) speech.
"""

from app.ai.voice.agents.breeze_buddy.accounts.rows import in_tenant
from app.ai.voice.agents.breeze_buddy.accounts.types import (
    SHAPES,
    Account,
    AzureAccount,
    BedrockAccount,
    GcpAccount,
    KeyAccount,
    VertexAccount,
    shape_problems,
)

__all__ = [
    "SHAPES",
    "Account",
    "AzureAccount",
    "BedrockAccount",
    "GcpAccount",
    "KeyAccount",
    "VertexAccount",
    "in_tenant",
    "shape_problems",
]
