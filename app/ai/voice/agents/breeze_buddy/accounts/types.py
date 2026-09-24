"""Typed accounts: a key never travels apart from its host.

The leaf shapes of the package — what a provider account IS, per vendor —
and ``SHAPES``, the vocabulary a credential row's ``provider`` must come from.
Imports nothing internal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type, Union, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

# A row's value is exactly its vendor's fields — never more. An unknown key
# (a `base_url` beside `endpoint`, say) would be dropped silently by
# pydantic's default, so a gateway host could hide where the host guard
# cannot see it and the key would travel to the public host instead.
_EXACT_FIELDS = ConfigDict(extra="forbid")


def _key_present(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("api_key is empty")
    return value


def _endpoint_is_a_url(value: Optional[str]) -> Optional[str]:
    """The shape's own check: a URL. Encrypted transport is a law on credential
    ROWS, applied where rows go through (account_from_value); a template's own
    endpoint on the env path may be an internal http:// gateway."""
    if value is None or value == "":
        return None
    if not value.startswith(("https://", "wss://", "http://", "ws://")):
        raise ValueError(f"endpoint {value!r} is not a URL")
    return value


def _endpoint_required(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("endpoint is empty")
    return str(_endpoint_is_a_url(value))


class KeyAccount(BaseModel):
    """An API-key account. ``endpoint`` is the host the key belongs to: a
    gateway (OpenAI-compatible), a residency cluster (ElevenLabs); None =
    the vendor's public host."""

    model_config = _EXACT_FIELDS

    api_key: str
    endpoint: Optional[str] = None

    _key_present = field_validator("api_key")(_key_present)
    _endpoint_is_a_url = field_validator("endpoint")(_endpoint_is_a_url)


class AzureAccount(BaseModel):
    """Azure OpenAI: the deployment endpoint is part of the account — a key
    without its endpoint is not an account."""

    model_config = _EXACT_FIELDS

    api_key: str
    endpoint: str

    _key_present = field_validator("api_key")(_key_present)
    _endpoint_required = field_validator("endpoint")(_endpoint_required)


class BedrockAccount(BaseModel):
    """AWS Bedrock: a Bedrock API key as the bearer token, or none — the
    pod's AWS credential chain. Region and model stay on the block."""

    model_config = _EXACT_FIELDS

    api_key: Optional[str] = None


class GcpAccount(BaseModel):
    """A Google service account (Cloud STT, Chirp TTS, Gemini TTS)."""

    model_config = _EXACT_FIELDS

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


class KeyOnlyAccount(BaseModel):
    """A key with no host of its own: the deployment decides the host
    (ElevenLabs — one url per service in the environment). An ``endpoint``
    on such a row is refused at the write rather than silently ignored."""

    model_config = _EXACT_FIELDS

    api_key: str

    _key_present = field_validator("api_key")(_key_present)


Account = Union[
    KeyAccount, AzureAccount, BedrockAccount, GcpAccount, VertexAccount, KeyOnlyAccount
]

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
    "elevenlabs": KeyOnlyAccount,  # host = the deployment's, per service (resolve.py)
    "cartesia": KeyAccount,
    "google": GcpAccount,
}


class AccountRefused(ValueError):
    """The block names an account it may not use, one that cannot serve the
    provider it names, or an environment that has no account for it. Fail
    closed: no service is built on it."""


class AccountShapeError(ValueError):
    """A row's value is not its vendor's shape; ``problems`` names each one."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def account_from_value(vendor: str, value: Any) -> Account:
    """PURE: the typed account a credential ROW's value is for its vendor —
    one validation, plus the law on rows: a shared secret travels over
    encrypted transport only. Raises AccountShapeError naming every problem
    (unknown vendor, a missing or extra field, a bad or plain-text endpoint)."""
    shape = SHAPES.get(vendor)
    if shape is None:
        raise AccountShapeError(
            [f"unknown provider {vendor!r}; one of {', '.join(sorted(SHAPES))}"]
        )
    try:
        account = shape.model_validate(value or {})
    except ValidationError as e:
        raise AccountShapeError(
            [
                f"{'.'.join(str(p) for p in err['loc']) or 'value'}: {err['msg']}"
                for err in e.errors()
            ]
        ) from e
    endpoint = getattr(account, "endpoint", None)
    if endpoint and not endpoint.startswith(("https://", "wss://")):
        raise AccountShapeError(
            [f"endpoint: {endpoint!r} is not an https:// or wss:// URL"]
        )
    return cast(Account, account)


def shape_problems(vendor: str, value: Any) -> List[str]:
    """PURE: what a row's value lacks for its vendor, empty when complete —
    the credential API asks this on create and update."""
    try:
        account_from_value(vendor, value)
    except AccountShapeError as e:
        return e.problems
    return []
