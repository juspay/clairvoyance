"""The engine class registry — the only vocabulary code owns.

WHICH engine an agent uses is stated in its evaluation_config row
(``configuration.engine``); this dict only maps that name to a class.
A second engine is one class + one entry here, with no change to the
worker, tables, API, or analytics.
"""

from typing import Dict

from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.base import (
    ConversationEvalsEngine,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.conversation_evals.engines.jev import (
    JevEngine,
)

ENGINES: Dict[str, ConversationEvalsEngine] = {"jev": JevEngine()}
