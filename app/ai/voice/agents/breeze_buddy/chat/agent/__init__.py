"""ChatAgent package — ``agent.py`` split by subsystem (2026-08-05).

The import surface is unchanged: ``from ...chat.agent import ChatAgent``.
One class, one instance per turn; submodules are subsystem mixins over
it (core / cycle / tooling / context / approval / render_ui / direct)
plus the shared ``runtime`` helpers. Tests that patch persistence or
streaming seams patch the submodule whose code makes the call.
"""

from app.ai.voice.agents.breeze_buddy.chat.agent.core import (  # noqa: F401
    ChatAgent as ChatAgent,
)
from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import (  # noqa: F401
    _chip_labels as _chip_labels,
    _KbMessage as _KbMessage,
    _partition_gated_calls as _partition_gated_calls,
    _PreparedTools as _PreparedTools,
)
