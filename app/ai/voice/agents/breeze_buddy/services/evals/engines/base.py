"""The eval engine base — one pipeline, four steps, every engine the same.

An engine turns a finished conversation into the common verdict shape.
The pipeline is fixed here and ``evaluate`` runs it; an engine fills in
the steps that differ:

  validate_configuration  the engine-owned part of a config row
  build_request           state + configuration -> the document the
                          provider sends (``ProviderRequest``)
  transform               the provider's reply -> the verdict that is
                          STORED (``ProviderResponse`` -> metadata dict)

``build_state`` (the projection every judge sees, the same for a call and
a chat) and ``Verdict`` (the stored structure) are shared, in ``common``,
and not an engine's to change. Everything an engine needs to know (questions,
thresholds, model) arrives in ``configuration`` — the agent's
evaluation_config row — never from code. Analytics reads only the common
shape and never knows which engine ran.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, FrozenSet, Mapping

from pydantic import BaseModel

from app.ai.voice.agents.breeze_buddy.services.evals.engines.common import (
    Verdict,
    build_state,
)
from app.ai.voice.agents.breeze_buddy.services.evals.providers import (
    EvalProvider,
    ProviderRequest,
    ProviderResponse,
)
from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel


class EvalEngine(ABC):
    # the vocabulary word the config row's ``engine`` is matched against
    name: ClassVar[str]
    channels: ClassVar[FrozenSet[ConversationChannel]]
    # the vendors this engine can be served by, by name; the config row's
    # `provider` is validated against the keys and routed to the value
    providers: ClassVar[Mapping[str, EvalProvider]]
    # the engine-owned top-level configuration keys beyond the envelope
    # (engine, provider, model): the type-level validator rejects any other
    configuration_keys: ClassVar[FrozenSet[str]]

    @abstractmethod
    def validate_configuration(self, configuration: Mapping[str, object]) -> BaseModel:
        """Decode the row into this engine's own configuration type (every
        eval is different: the structured judge's is
        ``StructuredJudgeConfiguration``). The type-level validator checks
        the envelope (engine, provider, model) first and then calls this.
        Raise ``ValueError``; insert nothing."""

    @abstractmethod
    def build_request(
        self, state: Dict[str, Any], configuration: Dict[str, Any]
    ) -> ProviderRequest:
        """PURE: the document the provider sends for this state. No I/O."""

    @abstractmethod
    def transform(
        self, response: ProviderResponse, configuration: Dict[str, Any]
    ) -> Verdict:
        """PURE: the provider's reply -> the ``Verdict`` that is stored.
        Runs before the adapter saves; raise on a reply that cannot be read.
        """

    async def evaluate(
        self, context: Dict[str, Any], configuration: Dict[str, Any]
    ) -> Verdict:
        """The pipeline: state -> request -> provider -> transform."""
        state = build_state(context)
        # the row picked the vendor; the validator guaranteed it is one of ours
        provider = self.providers[configuration["provider"]]
        response = await provider.call(self.build_request(state, configuration))
        return self.transform(response, configuration)
