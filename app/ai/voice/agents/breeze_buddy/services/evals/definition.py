"""The evals configuration definition: validation only.

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
whose primitives they are: the structured engine's score/choice/noul rules are its own, not
the type's.
"""

import copy
from typing import Mapping

from pydantic import BaseModel

from app.ai.voice.agents.breeze_buddy.services.evals.engines import ENGINES

# the envelope every row shares; each engine adds its own keys
# (``configuration_keys``: structured -> questions) and decodes the whole
# row into its own configuration type
_ENVELOPE_KEYS = frozenset({"engine", "provider", "model"})


def validate_evals_configuration(configuration: object) -> BaseModel:
    """Validate an evals configuration before it is stored.

    Envelope here, the rest in ``engine.validate_configuration``, which
    decodes the row into that engine's own configuration type
    (``StructuredJudgeConfiguration`` for ``structured``) and returns it.
    Raises ``ValueError`` with a caller-readable message. Nothing is
    defaulted or filled in — a missing piece is an error, never a silent
    substitution.
    """
    if not isinstance(configuration, Mapping):
        raise ValueError("configuration must be an object")

    config = copy.deepcopy(dict(configuration))

    engine = config.get("engine")
    # isinstance first: a list/object here would raise TypeError on the
    # dict lookup and surface as a 500 instead of a 400
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; available: {sorted(ENGINES)}")
    engine_class = ENGINES[engine]
    unknown = set(config) - _ENVELOPE_KEYS - engine_class.configuration_keys
    if unknown:
        raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
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

    return engine_class.validate_configuration(config)
