"""The engine class registry — the only vocabulary code owns.

WHICH engine an agent uses is stated in its evaluation_config row
(``configuration.engine``); this dict only maps that name to a class.
A new engine is one class + one entry here, with no change to the
worker, tables, API, or analytics.

  structured  typed questions WITH criteria (score rubric levels, described
              options) — read natively by jev on TypeSafe
  prompt      the same questions WITHOUT criteria: the scoring conditions
              are written in each question's instructions — chat models only
"""

from typing import Dict

from app.services.evals.engines.base import (
    EvalEngine,
)
from app.services.evals.engines.prompt_judge_engine import (
    PromptJudgeEngine,
)
from app.services.evals.engines.structured_judge_engine import (
    StructuredJudgeEngine,
)

ENGINES: Dict[str, EvalEngine] = {
    "structured": StructuredJudgeEngine(),
    "prompt": PromptJudgeEngine(),
}
