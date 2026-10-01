"""The eval provider contract — one vendor behind one call.

Every vendor is the same kind of thing at this level: a document goes in,
a document comes back. What the document MEANS belongs to the engine
(the structured judge knows state and questions; a prompt judge knows prompts); the provider
only knows how to deliver text to its vendor — pooled, retried, timed —
and hand the vendor's text back. One contract, one class per vendor."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    # the system message for chat vendors; None for a vendor that takes
    # no instruction (TypeSafe)
    instruction: Optional[str]
    # the document the engine built: a prompt's user message, or a JSON body
    content: str
    # call options the engine chose (temperature, max_output_tokens, stream,
    # region ...); a provider ignores what it has no use for
    settings: Mapping[str, Any]


@dataclass(frozen=True)
class ProviderResponse:
    # the vendor's raw output, untouched
    text: str
    # the model that actually served (the requested one is in the request)
    model: str
    usage: Optional[Mapping[str, Any]] = None


class EvalProvider(Protocol):
    # the vocabulary word the config row's ``provider`` is matched against
    name: str

    async def call(self, request: ProviderRequest) -> ProviderResponse:
        """Deliver one request to the vendor and return its answer. Retries
        and timeouts belong here; the fail posture belongs to the caller —
        raise on failure, never return a partial answer."""
        ...

    async def close(self) -> None:
        """Drain this provider's pooled client. Called once at process
        shutdown through ``close_eval_provider_pools``; safe when never opened.
        """
        ...
