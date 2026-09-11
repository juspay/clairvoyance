"""Virtual try-on for the Buddy Assist widget.

Clairvoyance owns the money and the gates; nautilus owns the pixels.

The photo passes through in memory and is never persisted here — not to
``chat_message``, ``ui_blocks``, ``agent_session_state``, or a log line.
The only try-on state kept is a counter in the session state and, for ten
minutes, one generated image keyed by ``request_id`` (see ``cache``).
"""

from app.services.breeze_buddy.try_on.cache import (
    cache_try_on_result,
    get_cached_try_on_result,
)
from app.services.breeze_buddy.try_on.client import (
    TryOnGenerationError,
    generate_try_on_image,
)

__all__ = [
    "TryOnGenerationError",
    "cache_try_on_result",
    "generate_try_on_image",
    "get_cached_try_on_result",
]
