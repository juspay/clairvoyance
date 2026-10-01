"""
Callback handlers for end_conversation events.
"""

# Imported for its side effect: registers the opt-in call.outcome_evaluated
# merchant webhook on the lead store's evaluated hook.
from app.ai.voice.agents.breeze_buddy.callbacks import (  # noqa: F401
    outcome_evaluated,
)
from app.ai.voice.agents.breeze_buddy.callbacks.service_callback import (
    service_callback,
)

__all__ = ["service_callback"]
