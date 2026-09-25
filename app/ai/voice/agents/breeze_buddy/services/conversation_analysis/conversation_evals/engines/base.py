"""The CONVERSATION_EVALS engine contract.

An engine turns a finished conversation into the common verdict shape;
everything it needs to know (questions, thresholds, model) arrives in the
``configuration`` argument — the agent's evaluation_config row — never
from code. Analytics reads only the common shape and never knows which
engine ran.
"""

from typing import Any, Dict, FrozenSet, Mapping, Protocol

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.providers import (
    ConversationEvalsProvider,
)
from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel

# The result-site label every engine's verdict carries as ``type``:
# evaluation_result.result and metadata->>'type' (the identity CHECK compares
# them). One value for the whole CONVERSATION_EVALS type, whatever engine ran.
RESULT_TYPE = "CONVERSATION_EVALS"


class ConversationEvalsEngine(Protocol):
    name: str
    channels: FrozenSet[ConversationChannel]
    # the vendors this engine can be served by, by name; the config row's
    # `provider` is validated against the keys and routed to the value
    # ("jev" today runs only on "typesafe")
    providers: Mapping[str, ConversationEvalsProvider]

    def validate_configuration(self, configuration: Dict[str, Any]) -> None:
        """Validate the engine-owned part of a configuration (``thresholds``,
        ``questions`` — whatever this engine's primitives are). The type-level
        validator checks only the engine-agnostic envelope (engine, provider,
        model) and then calls this. Raise ``ValueError``; insert nothing."""
        ...

    async def evaluate(
        self,
        context: Dict[str, Any],
        configuration: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return the common verdict shape:

        { type, engine, provider, model,
          answers{question_id: {score | choice, confidence}},
          ...engine-specific extras }
        """
        ...
