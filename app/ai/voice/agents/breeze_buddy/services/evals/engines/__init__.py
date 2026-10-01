"""The engine class registry — the only vocabulary code owns.

WHICH engine an agent uses is stated in its evaluation_config row
(``configuration.engine``); this dict only maps that name to a class.
A second engine is one class + one entry here, with no change to the
worker, tables, API, or analytics.
"""

from typing import Dict

from app.ai.voice.agents.breeze_buddy.services.evals.engines.base import (
    EvalEngine,
)
from app.ai.voice.agents.breeze_buddy.services.evals.engines.structured_judge_engine import (
    StructuredJudgeEngine,
)

ENGINES: Dict[str, EvalEngine] = {"structured": StructuredJudgeEngine()}
