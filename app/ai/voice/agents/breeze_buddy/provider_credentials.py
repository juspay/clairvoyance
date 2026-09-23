"""Provider accounts a template runs on (23–24 Sep 2026).

A template names its LLM, STT and TTS provider and model, and — with a
``credential_id`` on that block — the ACCOUNT: a credentials-table row whose
``provider`` matches and whose value holds what that provider needs. One
document can then be copied and pointed at another account by changing one
id. Without the word every key comes from the environment, exactly as
before.

The shape (review, 24 Sep 2026): the account reference stays ON the block,
the account carries its own host, and ONE resolver per call — ``Accounts``,
built from the call's tenant — answers every factory's question, "which
account does this block run on?", from the row when the block names one
and from the environment when it does not. The factories read the key and
the host from a typed account and never touch an environment variable
themselves, so a row can never be right for one service and wrong for
another, and a key never travels apart from its host.

Fail closed: a row is used only when it exists, is active, names the SAME
provider the block names, sits in the caller's tenant (its merchant, its
reseller, or global), carries that provider's fields, and can serve the
service asking (an OpenAI gateway account cannot serve OpenAI STT, which
has no gateway). Anything else raises — a call on the wrong account, or on
another tenant's account, is never placed. ``Accounts.problems`` asks the
same questions at template save, so a bad reference is a 422, not a dead
call.

Vocabulary lives here, in code, never in a CHECK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, Union

from pydantic import BaseModel, ValidationError, field_validator

from app.ai.voice.agents.breeze_buddy.template.types import (
    STTConfiguration,
    TTSConfig,
    TTSProvider,
)
from app.ai.voice.llm.types import LLMConfiguration, RealtimeConfig
from app.core.config import static
from app.core.config.dynamic import (
    AZURE_OPENAI_REALTIME_API_KEY,
    AZURE_OPENAI_REALTIME_ENDPOINT,
    BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY,
    GOOGLE_VERTEX_CREDENTIALS_JSON,
    GOOGLE_VERTEX_PROJECT_ID,
    OPENAI_REALTIME_API_KEY,
    XAI_REALTIME_API_KEY,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id
from app.schemas import Credential
from app.services.live_config.store import get_config

ELEVENLABS_PUBLIC_HOST = "wss://api.elevenlabs.io"


# --- typed accounts: a key never travels apart from its host -----------------


def _key_present(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("api_key is empty")
    return value


def _endpoint_is_a_url(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    if not value.startswith(("https://", "wss://", "http://", "ws://")):
        raise ValueError(f"endpoint {value!r} is not a URL")
    return value


class KeyAccount(BaseModel):
    """An API-key account. ``endpoint`` is the host the key belongs to: a
    gateway (OpenAI-compatible), a residency cluster (ElevenLabs); None =
    the vendor's public host."""

    api_key: str
    endpoint: Optional[str] = None

    _key_present = field_validator("api_key")(_key_present)
    _endpoint_is_a_url = field_validator("endpoint")(_endpoint_is_a_url)


class AzureAccount(BaseModel):
    """Azure OpenAI: the deployment endpoint is part of the account — a key
    without its endpoint is not an account."""

    api_key: str
    endpoint: str

    _key_present = field_validator("api_key")(_key_present)
    _endpoint_is_a_url = field_validator("endpoint")(_endpoint_is_a_url)


class BedrockAccount(BaseModel):
    """AWS Bedrock: a Bedrock API key as the bearer token, or none — the
    pod's AWS credential chain. Region and model stay on the block."""

    api_key: Optional[str] = None


class GcpAccount(BaseModel):
    """A Google service account (Cloud STT, Chirp TTS, Gemini TTS)."""

    credentials_json: str

    @field_validator("credentials_json")
    @classmethod
    def _present(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("credentials_json is empty")
        return value


class VertexAccount(GcpAccount):
    """Vertex AI: the service account plus its project."""

    project_id: str


Account = Union[KeyAccount, AzureAccount, BedrockAccount, GcpAccount, VertexAccount]

# vendor (a credential row's `provider`) -> the shape its value must have.
SHAPES: Dict[str, Type[BaseModel]] = {
    # text LLM
    "azure_openai": AzureAccount,
    "openai": KeyAccount,  # endpoint = an OpenAI-compatible gateway
    "google_vertex": VertexAccount,
    "aws_bedrock": BedrockAccount,
    # realtime LLM
    "openai_realtime": KeyAccount,
    "xai_realtime": KeyAccount,
    "azure_openai_realtime": AzureAccount,
    "gemini": KeyAccount,
    # STT / TTS
    "deepgram": KeyAccount,
    "soniox": KeyAccount,
    "sarvam": KeyAccount,
    "assemblyai": KeyAccount,
    "elevenlabs": KeyAccount,  # host = the deployment's cluster, see below
    "cartesia": KeyAccount,
    "google": GcpAccount,
}

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
# STT and TTS provider words already ARE the vendor names — except the
# Google speech services and Gemini TTS, which all run on one service
# account. DragonTTS is a proxy over a nested provider and is unwrapped
# BEFORE a block reaches here (unwrap_dragontts).
SPEECH_VENDOR: Dict[str, str] = {
    "deepgram": "deepgram",
    "soniox": "soniox",
    "sarvam": "sarvam",
    "assemblyai": "assemblyai",
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
    "openai": "openai",
    "google": "google",
    "gemini": "google",
}


class AccountRefused(ValueError):
    """The block names an account it may not use, one that cannot serve the
    provider it names, or an environment that has no account for it. Fail
    closed: no service is built on it."""


def _word(value: Any) -> Optional[str]:
    return getattr(value, "value", value)


def kind_of(block: Any) -> str:
    """Which service a block configures: llm · realtime · stt · tts."""
    if isinstance(block, RealtimeConfig):
        return "realtime"
    if isinstance(block, LLMConfiguration):
        return "llm"
    if isinstance(block, STTConfiguration):
        return "stt"
    if isinstance(block, TTSConfig):
        return "tts"
    raise TypeError(f"not a provider block: {type(block).__name__}")


def vendor_of(block: Any) -> str:
    """PURE: the vendor a block's provider word names. An unknown word is
    named ``<kind>:<word>`` so a refusal can say so; no block = Azure,
    today's default for the text LLM."""
    kind = kind_of(block)
    word = _word(getattr(block, "provider", None))
    if kind == "llm":
        word = word or "azure"
        return LLM_VENDOR.get(word) or f"llm:{word}"
    if kind == "realtime":
        return REALTIME_VENDOR.get(str(word)) or f"realtime:{word}"
    return SPEECH_VENDOR.get(str(word)) or f"{kind}:{word}"


def unwrap_dragontts(voice: TTSConfig) -> TTSConfig:
    """PURE: a DragonTTS voice WITH an account is synthesized by its nested
    provider directly (the proxy holds its own keys and would bill its own
    account), so the block becomes the nested provider's — once, here, and
    the account is checked against the provider that really synthesizes.
    A DragonTTS voice without an account is left alone: the proxy path."""
    if _word(voice.provider) != TTSProvider.DRAGONTTS.value or not voice.credential_id:
        return voice
    nested, sep, model = (voice.model or "").partition(":")
    if not sep or not nested or not model:
        raise AccountRefused(
            "dragontts with a credential_id requires model '<provider>:<model>' "
            f"on the block, got {voice.model!r}"
        )
    try:
        provider = TTSProvider(nested)
    except ValueError as e:
        raise AccountRefused(f"dragontts nests an unknown provider {nested!r}") from e
    return voice.model_copy(update={"provider": provider, "model": model})


def account_blocks(configurations: Any) -> Iterator[Tuple[str, Any]]:
    """Every block of a template that may name an account, by name — the
    one walk the save-time check and the observers share. An observer's
    block with no provider of its own takes the template's."""
    if configurations is None:
        return
    llm = getattr(configurations, "llm_configurations", None)
    if llm is not None:
        yield "llm_configurations", llm
        realtime = getattr(llm, "realtime", None)
        if realtime is not None:
            yield "llm_configurations.realtime", realtime
    stt = getattr(configurations, "stt_configuration", None)
    if stt is not None:
        yield "stt_configuration", stt
    tts = getattr(configurations, "tts_configuration", None)
    if tts is not None:
        yield "tts_configuration", tts
    for key, override in (
        getattr(configurations, "tts_configuration_overrides", None) or {}
    ).items():
        yield f"tts_configuration_overrides.{key}", override
    base_provider = getattr(llm, "provider", None) if llm is not None else None
    for i, observer in enumerate(getattr(configurations, "observers", None) or []):
        obs_llm = getattr(observer, "llm", None)
        if obs_llm is None:
            continue
        if obs_llm.provider is None and base_provider is not None:
            obs_llm = obs_llm.model_copy(update={"provider": base_provider})
        yield f"observers[{i}].llm", obs_llm


def in_tenant(
    credential: Credential, reseller_id: Optional[str], merchant_id: Optional[str]
) -> bool:
    """PURE: may (reseller, merchant) use this row? Global rows (no
    reseller) may be used by anyone; a reseller row by that reseller; a
    merchant row only by that merchant. Compared as text."""
    if credential.reseller_id is None:
        return True
    if str(credential.reseller_id) != str(reseller_id or ""):
        return False
    if credential.merchant_id is None:
        return True
    return str(credential.merchant_id) == str(merchant_id or "")


def refusal(
    credential: Optional[Credential],
    credential_id: str,
    vendor: str,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
) -> Optional[str]:
    """PURE: the one reason this row may not serve, or None."""
    if credential is None:
        return f"credential {credential_id} does not exist"
    if not credential.is_active:
        return f"credential {credential_id} is inactive"
    if not in_tenant(credential, reseller_id, merchant_id):
        return f"credential {credential_id} belongs to another tenant"
    if credential.provider != vendor:
        return (
            f"credential {credential_id} is for {credential.provider or 'no'} "
            f"provider, this block needs {vendor}"
        )
    return None


def shape_problems(vendor: str, value: Any) -> List[str]:
    """PURE: what a row's value lacks for its vendor, empty when complete.
    The credential API asks this on create and update; the resolver asks
    it again per call."""
    shape = SHAPES.get(vendor)
    if shape is None:
        return [f"unknown provider {vendor!r}; one of {', '.join(sorted(SHAPES))}"]
    try:
        shape.model_validate(value or {})
    except ValidationError as e:
        return [
            f"{'.'.join(str(p) for p in err['loc']) or 'value'}: {err['msg']}"
            for err in e.errors()
        ]
    return []


async def elevenlabs_host() -> str:
    """The ElevenLabs cluster this deployment runs on: the India-resident
    one when BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY is on (true by default),
    else the public one. Every ElevenLabs account a deployment names lives
    on that cluster — a row carries only its key (ruled 24 Sep 2026)."""
    if await BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY():
        return static.ELEVENLABS_INDIAN_RESIDENCY_WEBSOCKET_URL
    return ELEVENLABS_PUBLIC_HOST


async def env_account(vendor: str, kind: str, block: Any) -> Account:
    """The environment's account for a vendor — the ONLY place an env key
    is read for a call. ``api_key_name`` (a named dynamic-config key) lives
    here too. Raises AccountRefused with the vendor's own words when the
    environment has none."""
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
        return AzureAccount(
            api_key=key or "", endpoint=endpoint or static.AZURE_OPENAI_ENDPOINT
        )
    if vendor == "openai" and kind == "llm":
        key = await named_key("OpenAI") or static.OPENAI_API_KEY
        return KeyAccount(api_key=key or "", endpoint=endpoint)
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
        if kind == "stt":
            # Scribe has one host; the env key for it, as before.
            if not static.ELEVENLABS_API_KEY:
                raise AccountRefused(
                    "ELEVENLABS_API_KEY is required for elevenlabs STT"
                )
            return KeyAccount(api_key=str(static.ELEVENLABS_API_KEY))
        host = await elevenlabs_host()
        if host == ELEVENLABS_PUBLIC_HOST:
            if not static.ELEVENLABS_API_KEY:
                raise AccountRefused("ELEVENLABS_API_KEY is not set")
            return KeyAccount(api_key=str(static.ELEVENLABS_API_KEY), endpoint=host)
        if not static.ELEVENLABS_INDIAN_RESIDENCY_API_KEY:
            raise AccountRefused(
                "ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required when "
                "BB_ENABLE_ELEVENLABS_INDIAN_RESIDENCY is True"
            )
        return KeyAccount(
            api_key=str(static.ELEVENLABS_INDIAN_RESIDENCY_API_KEY), endpoint=host
        )
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

    async def get(self, block: Any) -> Any:
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

    async def _row(self, vendor: str, kind: str, credential_id: str) -> Any:
        credential = await get_credential_by_id(
            credential_id, mask=False, raise_errors=True
        )
        reason = refusal(
            credential, credential_id, vendor, self.reseller_id, self.merchant_id
        )
        if reason:
            raise AccountRefused(reason)
        assert credential is not None
        lacking = shape_problems(vendor, credential.value)
        if lacking:
            raise AccountRefused(
                f"credential {credential_id} is incomplete for {vendor}: "
                + ", ".join(lacking)
            )
        account: Any = SHAPES[vendor].model_validate(credential.value or {})
        # The host a service can actually use (review #8): a service with
        # one fixed host cannot take an account that lives elsewhere.
        if vendor == "elevenlabs":
            if kind == "stt":
                if await elevenlabs_host() != ELEVENLABS_PUBLIC_HOST:
                    raise AccountRefused(
                        f"credential {credential_id}: this deployment's ElevenLabs "
                        "accounts live on the India-resident cluster, and "
                        "ElevenLabs STT can only use the public host"
                    )
                account = KeyAccount(api_key=account.api_key)
            else:
                # The deployment's cluster decides the host; the row is the key.
                account = KeyAccount(
                    api_key=account.api_key, endpoint=await elevenlabs_host()
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
            except (AccountRefused, ValidationError, ValueError) as e:
                found.append(f"{name}: {e}")
        return found


def accounts_for_template(template: Any) -> Accounts:
    """An ``Accounts`` in the template's own tenant — for the paths that
    have no lead: chat turns, the greeting job, template save."""
    return Accounts(
        reseller_id=getattr(template, "reseller_id", None),
        merchant_id=getattr(template, "merchant_id", None),
    )


__all__ = [
    "Account",
    "AccountRefused",
    "Accounts",
    "AzureAccount",
    "BedrockAccount",
    "ELEVENLABS_PUBLIC_HOST",
    "GcpAccount",
    "KeyAccount",
    "SHAPES",
    "VertexAccount",
    "account_blocks",
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
