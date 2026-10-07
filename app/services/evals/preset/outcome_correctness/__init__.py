"""The built-in outcome_correctness eval: which of the agent's own outcome
words the call should have ended with.

A global ``evaluation_config`` row (migration 083, named
``OUTCOME_CORRECTNESS``) holds its engine, model and threshold, and is the
default for every agent: off as seeded. An agent's own row of that name
overrides it, enabled or disabled. Its one
question is built per agent from the words the agent's template writes
(``app/services/evals/shared/utils/extract_agent_outcomes.py``), plus ``NO_DECISION``
(``question``). The call-time check
that runs it and saves a corrected outcome is Buddy's
(``conversation_analysis/preset/outcome_eval.py``); this package is pure.
"""

from app.database.queries.breeze_buddy.evaluation_config import OUTCOME_CORRECTNESS
from app.services.evals.preset.outcome_correctness.question import (
    NO_DECISION,
    corrected_outcome,
    outcome_answer,
    outcome_question,
    replaceable,
)

__all__ = [
    "NO_DECISION",
    "OUTCOME_CORRECTNESS",
    "corrected_outcome",
    "outcome_answer",
    "outcome_question",
    "replaceable",
]
