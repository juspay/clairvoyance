"""The LLM side: which vendor a text-LLM or realtime block names, and the
environment's account for it. ``api_key_name`` (a named dynamic-config key)
and ``account`` (a named deployment account) live here; both are LLM
words."""

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

# The deployment accounts a text-LLM block may name by ``account``. Each
# speaks the OpenAI API, so each serves the openai provider.
NAMED_ACCOUNTS = ("grid-topics",)
# Each named account's key, by its pod variable. A key serves only its own
# account, never a block that names it as its ``api_key_name``.
NAMED_ACCOUNT_KEYS = {
    "GRID_TOPICS_API_KEY": "grid-topics",
}


async def named_account(name: str, vendor: str, for_topics: bool = False) -> Account:
    """The deployment account a block names by ``account``. The block holds
    only the name; the host and the key are this deployment's own, and the
    key is read from the pod environment only (static), never from dynamic
    config, so a DevCycle value can never repoint it. ``grid-topics`` serves
    post-call topic evaluations only (``for_topics``): its key was split from
    live traffic's so that neither starves the other. Raises AccountRefused
    for an unknown name, another provider, a caller the account does not
    serve, or a missing host or key."""
    if name not in NAMED_ACCOUNTS:
        raise AccountRefused(
            f"no account named {name!r}; one of {', '.join(NAMED_ACCOUNTS)}"
        )
    if vendor != "openai":
        raise AccountRefused(
            f"account {name!r} serves the openai provider, this block needs "
            f"{vendor}"
        )
    if name == "grid-topics" and not for_topics:
        raise AccountRefused(
            "account 'grid-topics' serves topic evaluations only; its key is "
            "kept apart from live traffic"
        )
    # grid-topics: the Grid gateway on the topic evaluations' own key.
    endpoint = (await get_config("LITELLM_BASE_URL", "", str)).strip()
    if not endpoint:
        raise AccountRefused("LITELLM_BASE_URL is required for the grid-topics account")
    if not static.GRID_TOPICS_API_KEY:
        raise AccountRefused("GRID_TOPICS_API_KEY is not set in the pod environment")
    return KeyAccount(
        api_key=static.GRID_TOPICS_API_KEY,
        endpoint=endpoint.rstrip("/").removesuffix("/chat/completions"),
    )


async def env_account(vendor: str, kind: str, block: Any) -> Account:
    """The environment's LLM / realtime account for a vendor. A template's
    OWN ``endpoint`` (with its ``api_key_name``) is the author's contract, as
    today: taken as written, so an internal ``http://`` gateway keeps
    working — the https-only law is a law on credential ROWS. Raises
    AccountRefused, and only AccountRefused, when the environment has none."""
    endpoint = getattr(block, "endpoint", None) or None
    api_key_name = getattr(block, "api_key_name", None) or None

    async def named_key(label: str) -> Optional[str]:
        owner = NAMED_ACCOUNT_KEYS.get(api_key_name or "")
        if owner:
            raise AccountRefused(
                f"{api_key_name} is the {owner!r} account's key; it serves only "
                "that account and is never an api_key_name"
            )
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
