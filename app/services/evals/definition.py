"""The evals configuration definition: validation only.

WHAT runs for an agent lives in its ``evaluation_config`` rows, never in
code (Rabi's ruling), and there is no default configuration for the evals
an agent configures: no fallback in code. An agent with no row (or a
disabled one) simply does not run that evaluation; the admin configuration
POST (create-or-replace) is the only write and passes through this
validator. The one exception is the preset outcome_correctness eval, a
global row seeded by migration 083 that is the default for every agent (off
as seeded; an agent's own row of that name overrides it)
(conversation_analysis/preset/outcome_eval.py).

Code knows no question by name: whatever should be judged — the call's
outcome, loops, anything — is a question in the agent's configuration,
and its answer is stored as such. The validator checks only the
engine-agnostic envelope — a known engine, a provider that engine
supports, a model, and no key that neither the engine nor the provider
owns — and hands the rest to the engine class, whose primitives they are:
the structured engine's score/choice/noul rules are its own, not the
type's. Keys beyond the envelope are the engine's (``questions``; the
prompt engine's ``instruction`` and ``settings`` too).
"""

import copy
from typing import Mapping

from pydantic import BaseModel

from app.services.evals.engines import ENGINES

# the envelope every row shares; the engine adds its own keys
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
    provider_name = config.get("provider")
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ValueError("provider must be a non-empty string")
    if provider_name not in engine_class.providers:
        raise ValueError(
            f"engine {engine!r} does not support provider {provider_name!r}; "
            f"supported: {sorted(engine_class.providers)}"
        )
    unknown = set(config) - _ENVELOPE_KEYS - engine_class.configuration_keys
    if unknown:
        raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")

    return engine_class.validate_configuration(config)
