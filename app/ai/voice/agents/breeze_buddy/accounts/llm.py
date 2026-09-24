"""The LLM side: which vendor a text-LLM or realtime block names, and the
environment's account for it. ``api_key_name`` (a named dynamic-config key)
lives here — it is an LLM word."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.accounts.types import (
    Account,
    AccountRefused,
    AzureAccount,
    BedrockAccount,
    KeyAccount,
    VertexAccount,
)
from app.core.config import static
from app.core.config.dynamic import (
    AZURE_OPENAI_REALTIME_API_KEY,
    AZURE_OPENAI_REALTIME_ENDPOINT,
    GOOGLE_VERTEX_CREDENTIALS_JSON,
    GOOGLE_VERTEX_PROJECT_ID,
    OPENAI_REALTIME_API_KEY,
    XAI_REALTIME_API_KEY,
)
from app.services.live_config.store import get_config

# Which vendor each block's provider word names.
LLM_VENDOR: Dict[str, str] = {
    "azure": "azure_openai",
    "openai": "openai",
    "google_vertex": "google_vertex",
    "aws_bedrock": "aws_bedrock",
}
REALTIME_VENDOR: Dict[str, str] = {
    "openai": "openai_realtime",
    "xai": "xai_realtime",
    "azure": "azure_openai_realtime",
    "gemini": "gemini",
}


async def env_account(vendor: str, kind: str, block: Any) -> Account:
    """The environment's LLM / realtime account for a vendor. A template's
    OWN ``endpoint`` (with its ``api_key_name``) is the author's contract, as
    today: taken as written, so an internal ``http://`` gateway keeps
    working — the https-only law is a law on credential ROWS. Raises
    AccountRefused, and only AccountRefused, when the environment has none."""
    endpoint = getattr(block, "endpoint", None) or None
    api_key_name = getattr(block, "api_key_name", None) or None

    async def named_key(label: str) -> Optional[str]:
        # A custom endpoint without a named key would send the default key
        # to a third party — refused, as before.
        if endpoint and not api_key_name:
            raise AccountRefused(
                "api_key_name or credential_id is required when a custom "
                f"endpoint is provided for {label}"
            )
        if api_key_name:
            key = await get_config(api_key_name, "", str)
            if not key:
                raise AccountRefused(
                    f"API key not found for config key: {api_key_name}"
                )
            return key
        return None

    if vendor == "azure_openai":
        key = await named_key("Azure") or static.AZURE_OPENAI_API_KEY
        host = endpoint or static.AZURE_OPENAI_ENDPOINT
        if not key:
            raise AccountRefused("AZURE_OPENAI_API_KEY is required for the Azure LLM")
        if not host:
            raise AccountRefused("AZURE_OPENAI_ENDPOINT is required for the Azure LLM")
        return AzureAccount(api_key=str(key), endpoint=str(host))
    if vendor == "openai":
        key = await named_key("OpenAI") or static.OPENAI_API_KEY
        if not key:
            raise AccountRefused("OPENAI_API_KEY is required for the OpenAI LLM")
        return KeyAccount(api_key=str(key), endpoint=endpoint)
    if vendor == "google_vertex":
        credentials_json = await GOOGLE_VERTEX_CREDENTIALS_JSON()
        project_id = await GOOGLE_VERTEX_PROJECT_ID()
        if not credentials_json:
            raise AccountRefused(
                "GOOGLE_VERTEX_CREDENTIALS_JSON is required for google_vertex provider"
            )
        if not project_id:
            raise AccountRefused(
                "GOOGLE_VERTEX_PROJECT_ID is required for google_vertex provider"
            )
        return VertexAccount(
            credentials_json=str(credentials_json), project_id=str(project_id)
        )
    if vendor == "aws_bedrock":
        return BedrockAccount(api_key=await named_key("Bedrock"))
    if vendor == "openai_realtime":
        key = await OPENAI_REALTIME_API_KEY()
        if not key:
            raise AccountRefused(
                "OPENAI_REALTIME_API_KEY must be set in Redis dynamic config "
                "to use OpenAI Realtime"
            )
        return KeyAccount(api_key=key)
    if vendor == "xai_realtime":
        key = await XAI_REALTIME_API_KEY()
        if not key:
            raise AccountRefused(
                "XAI_REALTIME_API_KEY must be set in Redis dynamic config "
                "to use xAI Grok Realtime"
            )
        return KeyAccount(api_key=key)
    if vendor == "azure_openai_realtime":
        key = await AZURE_OPENAI_REALTIME_API_KEY()
        if not key:
            raise AccountRefused(
                "AZURE_OPENAI_REALTIME_API_KEY must be set in Redis dynamic "
                "config to use Azure Realtime"
            )
        host = endpoint or await AZURE_OPENAI_REALTIME_ENDPOINT()
        if not host:
            raise AccountRefused(
                "Azure Realtime requires a WebSocket endpoint URL — set it "
                "via llm_configurations.realtime.endpoint on the template or "
                "via AZURE_OPENAI_REALTIME_ENDPOINT in Redis dynamic config"
            )
        return AzureAccount(api_key=key, endpoint=host)
    if vendor == "gemini":
        if not static.GEMINI_API_KEY:
            raise AccountRefused(
                "GEMINI_API_KEY must be set in the environment "
                "to use Gemini Live Realtime"
            )
        return KeyAccount(api_key=str(static.GEMINI_API_KEY))
    raise AccountRefused(f"no provider account exists for {vendor.split(':', 1)[-1]}")


def row_account(vendor: str, account: Account, credential_id: str) -> Account:
    """A row on an LLM / realtime block is used as it is: its host is its own."""
    return account
