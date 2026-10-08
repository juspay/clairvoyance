"""The eval engine base — one pipeline, every engine the same.

An engine turns a finished conversation into the common verdict shape.
The pipeline is fixed here and ``evaluate`` runs it; an engine fills in
the steps that differ:

  validate_configuration  the engine-owned part of a config row
  build_request           state + configuration -> the request its model
                          reads (``GenerateRequest``)
  transform               the model's reply -> the verdict that is STORED

Each engine serves one kind of model end to end — the structured judge a
judge API that reads criteria natively (jev on TypeSafe), the prompt judge
a chat model — so no engine branches on a provider. The model itself is
behind a generic ``ModelProvider`` (app/services/model_provider) that
knows nothing about evaluations.

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

from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel
from app.services.evals.engines.common import (
    Verdict,
    build_state,
)
from app.services.model_provider import (
    GenerateRequest,
    GenerateResponse,
    ModelProvider,
)


class EvalEngine(ABC):
    # the vocabulary word the config row's ``engine`` is matched against
    name: ClassVar[str]
    channels: ClassVar[FrozenSet[ConversationChannel]]
    # the providers this engine can be served by, by name; the config row's
    # `provider` is validated against the keys and routed to the value
    providers: ClassVar[Mapping[str, ModelProvider]]
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
        self, state: Dict[str, Any], configuration: Mapping[str, Any]
    ) -> GenerateRequest:
        """PURE: the request this engine's model reads for this state."""

    @abstractmethod
    def transform(
        self, response: GenerateResponse, configuration: Mapping[str, Any]
    ) -> Verdict:
        """PURE: the model's reply -> the ``Verdict`` that is stored.
        Runs before the adapter saves; raise on a reply that cannot be read.
        """

    async def evaluate(
        self, context: Dict[str, Any], configuration: Dict[str, Any]
    ) -> Verdict:
        """The pipeline: state -> request -> provider -> transform."""
        state = build_state(context)
        # the row picked the provider; the validator guaranteed it is one of ours
        provider = self.providers[configuration["provider"]]
        response = await provider.generate(self.build_request(state, configuration))
        return self.transform(response, configuration)
