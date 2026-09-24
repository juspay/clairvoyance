"""One resolver per call: which account does this block run on?

``Accounts``, built from the CALL's tenant, answers every factory's question
from the row when the block names one (``credential_id``) and from the
environment when it does not. The factories read the key and the host from
a typed account and never touch an environment variable themselves, so a
row can never be right for one service and wrong for another, and a key
never travels apart from its host.

Fail closed: a row is used only when it exists, is active, names the SAME
provider the block names, sits in the caller's tenant (its merchant, its
reseller, or global), carries that provider's fields, and can serve the
service asking (an OpenAI gateway account cannot serve OpenAI STT, which
has no gateway). Anything else raises — a call on the wrong account, or on
another tenant's account, is never placed. ``Accounts.problems`` asks the
same questions at template save, so a bad reference is a 422, not a dead
call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.accounts.blocks import (
    AccountRefused,
    account_blocks,
    kind_of,
    unwrap_dragontts,
    vendor_of,
)
from app.ai.voice.agents.breeze_buddy.accounts.rows import refusal
from app.ai.voice.agents.breeze_buddy.accounts.types import (
    SHAPES,
    Account,
    AzureAccount,
    BedrockAccount,
    GcpAccount,
    KeyAccount,
    KeyOnlyAccount,
    VertexAccount,
    account_from_value,
)
from app.ai.voice.agents.breeze_buddy.template.types import TTSConfig
from app.core.config import static
from app.core.config.dynamic import (
    AZURE_OPENAI_REALTIME_API_KEY,
    AZURE_OPENAI_REALTIME_ENDPOINT,
    GOOGLE_VERTEX_CREDENTIALS_JSON,
    GOOGLE_VERTEX_PROJECT_ID,
    OPENAI_REALTIME_API_KEY,
    XAI_REALTIME_API_KEY,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id
from app.services.live_config.store import get_config


def elevenlabs_host(kind: str) -> str:
    """The ElevenLabs host this deployment dials for a service — one (url,
    key) pair per service in the environment, no flag (release cbcec339,
    24 Sep 2026): ``ELEVENLABS_STT_URL`` for STT, ``ELEVENLABS_TTS_URL`` for
    TTS, both bare hosts. Every ElevenLabs account a deployment names lives
    on that host — a row carries only its key. The account carries the host
    as a ``wss://`` URL; the TTS stream uses it verbatim, the batch path
    dials it as ``https://``, and the STT builder hands pipecat the bare
    host it expects."""
    host = static.ELEVENLABS_STT_URL if kind == "stt" else static.ELEVENLABS_TTS_URL
    return f"wss://{host}"


async def env_account(vendor: str, kind: str, block: Any) -> Account:
    """The environment's account for a vendor — the ONLY place an env key
    is read for a call. ``api_key_name`` (a named dynamic-config key) lives
    here too. Raises AccountRefused, and only AccountRefused, with the
    vendor's own words when the environment has none.

    A template's OWN ``endpoint`` (with its ``api_key_name``) is the
    author's contract, as today: it is taken as written, so an internal
    ``http://`` gateway keeps working. The https-only law is a law on
    credential ROWS (shared secrets), enforced where a row is written and
    read — not here."""
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
    if vendor == "openai" and kind == "llm":
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
    if vendor == "openai":  # STT
        if not static.OPENAI_STT_API_KEY:
            raise AccountRefused("OPENAI_STT_API_KEY is required for openai STT")
        return KeyAccount(api_key=str(static.OPENAI_STT_API_KEY))
    if vendor == "elevenlabs":
        # One (url, key) pair per service, from the env, as release reads it.
        if kind == "stt":
            key, name = static.ELEVENLABS_STT_API_KEY, "ELEVENLABS_STT_API_KEY"
        else:
            key, name = static.ELEVENLABS_TTS_API_KEY, "ELEVENLABS_TTS_API_KEY"
        if not key:
            raise AccountRefused(f"{name} is required for elevenlabs {kind.upper()}")
        return KeyAccount(api_key=str(key), endpoint=elevenlabs_host(kind))
    if vendor == "google":
        if not static.GOOGLE_CREDENTIALS_JSON:
            raise AccountRefused(f"GOOGLE_CREDENTIALS_JSON is required for {kind}")
        return GcpAccount(credentials_json=str(static.GOOGLE_CREDENTIALS_JSON))
    env_keys = {
        "deepgram": static.DEEPGRAM_API_KEY,
        "soniox": static.SONIOX_API_KEY,
        "sarvam": static.SARVAM_API_KEY,
        "assemblyai": static.ASSEMBLYAI_API_KEY,
        "cartesia": static.CARTESIA_API_KEY,
    }
    if vendor in env_keys:
        if not env_keys[vendor]:
            raise AccountRefused(
                f"{vendor.upper()}_API_KEY is required for {vendor} {kind.upper()}"
            )
        return KeyAccount(api_key=str(env_keys[vendor]))
    raise AccountRefused(f"no provider account exists for {vendor.split(':', 1)[-1]}")


@dataclass
class Accounts:
    """The provider accounts one call runs on. Built at the entry point from
    the CALL's tenant (the lead's, kept across transfers; the template's for
    chat, the greeting and IVR) and passed down; each block is resolved
    lazily, once, so a chat turn reads at most the LLM row."""

    reseller_id: Optional[str] = None
    merchant_id: Optional[str] = None
    _memo: Dict[Tuple[Any, ...], Account] = field(default_factory=dict)

    async def get(self, block: Any) -> Account:
        """The account this block runs on — its row when it names one, else
        the environment's. Raises AccountRefused."""
        if isinstance(block, TTSConfig):
            block = unwrap_dragontts(block)
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
        # The host a service can actually use: a service with one fixed
        # host cannot take an account that lives elsewhere. ElevenLabs: the
        # deployment's per-service host decides; the row is the key.
        if vendor == "elevenlabs":
            assert isinstance(account, KeyOnlyAccount)
            account = KeyAccount(
                api_key=account.api_key, endpoint=elevenlabs_host(kind)
            )
        elif (
            vendor == "openai" and kind == "stt" and getattr(account, "endpoint", None)
        ):
            raise AccountRefused(
                f"credential {credential_id} names a gateway endpoint, and "
                "OpenAI STT has no gateway — it can only use the public host"
            )
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
