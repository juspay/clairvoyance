"""The CONVERSATION_EVALS evaluation definition: validation only.

WHAT runs for an agent lives in its ``evaluation_config`` row, never in
code (Rabi's ruling), and there is NO default configuration anywhere: no
seed in the DB, no fallback in code. An agent with no row (or a disabled
one) simply does not run this evaluation; the admin configuration POST
(create-or-replace) is the only write and passes through this validator.

Code knows no question by name: whatever should be judged — the call's
outcome, loops, anything — is a question in the agent's configuration,
and its answer is stored as such. The validator checks only the
engine-agnostic envelope — allowed keys, a known engine, a provider that
engine supports, a model — and hands ``questions`` to the engine class,
whose primitives they are: Jev's score/choice/noul rules are Jev's, not
the type's.
"""

import copy
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines import (
    ENGINES,
)

_ALLOWED_KEYS = {"engine", "provider", "model", "questions"}


def validate_conversation_evals_configuration(configuration: Any) -> Dict[str, Any]:
    """Validate a CONVERSATION_EVALS configuration before it is stored.

    Envelope here, engine-owned content in ``engine.validate_configuration``.
    Raises ``ValueError`` with a caller-readable message; returns a deep
    copy of the configuration. Nothing is defaulted or filled in — a
    missing piece is an error, never a silent substitution.
    """
    if not isinstance(configuration, dict):
        raise ValueError("configuration must be an object")
    unknown = set(configuration) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown configuration keys: {sorted(unknown)}")

    config = copy.deepcopy(configuration)

    engine = config.get("engine")
    # isinstance first: a list/object here would raise TypeError on the
    # dict lookup and surface as a 500 instead of a 400
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; available: {sorted(ENGINES)}")
    engine_class = ENGINES[engine]
    provider = config.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if provider not in engine_class.providers:
        raise ValueError(
            f"engine {engine!r} does not support provider {provider!r}; "
            f"supported: {sorted(engine_class.providers)}"
        )
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")

    engine_class.validate_configuration(config)
    return config
