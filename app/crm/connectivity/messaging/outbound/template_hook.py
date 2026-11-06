"""The template hook slot — what runs after a template is queued on a
channel with conversations, filled at the composition root (worker_main).

Connectivity may not import conversations (conversations already reads this
module's contracts; the reverse arrow would close an import cycle), so the
slot is filled from outside, as the retire guard is. Conversations puts the
template on the customer's thread when Buddy's binding is also the template
binding. Empty means nobody listens, and queueing carries on.

Fire-and-forget: the hook only shows a send on a thread, so a hook that
raises is logged and never costs the send.
"""

from datetime import datetime
from typing import Awaitable, Callable, Optional

from app.core.logger import logger

#: (merchant_id, channel, customer_id, message_id, template_id, source_kind,
#: queued_at) -> None
TemplateHook = Callable[
    [str, str, str, str, Optional[str], str, datetime], Awaitable[None]
]
_template_hook: Optional[TemplateHook] = None


def register_template_hook(hook: TemplateHook) -> None:
    """Idempotent: imports can run more than once (tests, reload)."""
    global _template_hook
    _template_hook = hook


async def call_template_hook(
    merchant_id: str,
    channel: str,
    customer_id: str,
    message_id: str,
    template_id: Optional[str],
    source_kind: str,
    queued_at: datetime,
) -> None:
    """Run the registered hook for one newly queued template; never raises."""
    if _template_hook is None:
        return
    try:
        await _template_hook(
            merchant_id,
            channel,
            customer_id,
            message_id,
            template_id,
            source_kind,
            queued_at,
        )
    except Exception as e:  # noqa: BLE001 — see the module docstring
        logger.opt(exception=e).warning(
            f"template hook failed for message {message_id}"
        )
