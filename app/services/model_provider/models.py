"""The provider-neutral shapes: what a caller sends, what it gets back.

Nothing here knows a vendor. Each provider translates ``GenerateRequest``
into its vendor's wire format and the vendor's reply back into
``GenerateResponse`` — a caller switches vendor by switching provider,
never by changing its request.
"""

from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional, Sequence, Union

Role = Literal["user", "assistant"]

# a JSON Schema object (draft 2020-12 keywords); each provider translates it
# to its vendor's structured-output form
JsonSchema = Mapping[str, object]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


# what the model is given: a conversation for a chat model, or a structured
# payload for a model with a task-specific API (TypeSafe: state + questions).
# A provider raises ``ProviderError`` on the kind it does not take
Input = Union[Sequence[Message], Mapping[str, object]]


@dataclass(frozen=True)
class GenerationSettings:
    """The common knobs; None = the vendor's default. Each provider maps
    them to its vendor's field names."""

    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stop: Optional[Sequence[str]] = None


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    input: Input
    system_prompt: Optional[str] = None
    # the shape the reply must have; None = free text
    schema: Optional[JsonSchema] = None
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    # vendor-only body fields, passed through as given; they never override
    # what the request above already sets
    extra: Optional[Mapping[str, object]] = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class GenerateResponse:
    # the reply text as the model gave it
    content: str
    # the model that actually served (may differ from the requested one)
    model: str
    # the reply parsed as JSON when it is structured, else None
    structured: Optional[object] = None
    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None


@dataclass(frozen=True)
class ProviderConfig:
    """How a provider reaches its vendor; built once from static config."""

    api_key: str
    base_url: str
    timeout_seconds: float
    # retries after the first attempt, for what a retry can fix (429, 5xx,
    # transport errors)
    max_retries: int = 2


class ProviderError(Exception):
    """A generate call failed (after retries, when retryable)."""

    def __init__(
        self,
        provider: str,
        message: str,
        status: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status
        self.retryable = retryable
