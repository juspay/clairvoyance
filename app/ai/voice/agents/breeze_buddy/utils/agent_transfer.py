"""Agent-to-agent transfer support types + helpers.

``PendingAgentTransfer`` is the handoff payload the ``connect_to_agent`` handler
stashes on the bot; the ``Agent.run`` generation loop consumes it to rebuild the
pipeline from the target template. ``suppress_auto_hangup`` stops the telephony
serializer from REST-hanging-up the live call when the outgoing pipeline's EndFrame
drains during a transfer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.core.logger import logger

if TYPE_CHECKING:
    from pipecat.runner.types import RunnerArguments

    from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel


@dataclass
class PendingAgentTransfer:
    """Handoff payload for one agent-to-agent transfer.

    Set by the ``connect_to_agent`` handler once the target template has loaded,
    rendered, and validated; consumed by ``Agent.run``'s generation loop.
    """

    template: TemplateModel  # loaded + rendered + validated target template
    template_vars: Dict[str, str]
    handoff_messages: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class TransportRebuildContext:
    """Everything ``apply_transfer`` needs to rebuild a FRESH transport over the
    same live connection on each generation.

    Captured once at pipeline setup and read on every transfer. pipecat transports
    latch an ``_initialized`` flag and cannot be restarted, and the telephony
    provider type + call_data come only from the one-time opening handshake
    (unreadable on the mid-stream socket for generation >= 2), so the rebuild
    replays what it needs from here: telephony reuses ``ws_proxy`` and replays
    ``telephony_transport_type`` + ``telephony_call_data``; daily replays
    ``runner_args``.

    Why only these four fields are grouped (and the other transfer/generation
    state on ``Agent`` — ``pending_transfer``, ``transfer_count``, ``generation``,
    ``prior_generation_messages``, ``_handoff_messages``, ``_flow_initialized`` —
    stays flat): these four form a single cohesive *value* — the argument bundle
    for one operation, "rebuild the transport." They share one lifecycle (written
    once at setup, read once in ``apply_transfer``) and are literally the inputs to
    ``_create_telephony_transport`` / ``create_transport``, so bundling them makes
    a real object. The other fields are heterogeneous lifecycle/signal state (a
    handoff baton, counters, message lists, a guard flag) touched across the
    handler, the run loop, ``apply_transfer``, ``_handle_client_connected`` and
    ``end_conversation``; wrapping them would add indirection and churn in many
    files without forming a meaningful value, so they intentionally stay flat.
    """

    ws_proxy: Any = None  # NonClosingWebSocket over the raw call ws
    runner_args: Optional[RunnerArguments] = None  # daily rebuild
    telephony_transport_type: Optional[str] = None  # telephony rebuild
    telephony_call_data: Optional[dict] = None


def suppress_auto_hangup(transport: Any) -> None:
    """Stop the telephony serializer from REST-hanging-up the call on EndFrame.

    Generalizes the warm-transfer Plivo trick (``handlers/internal/warm_transfer.py``
    sets ``serializer._hangup_attempted = True``) to all providers.
    ``_hangup_attempted`` is a private pipecat attribute — this helper is the one
    deliberately version-coupled seam in the transfer feature (verified present and
    identical on pipecat 1.1.0). Daily transports have no serializer; the ``hasattr``
    guard makes this a no-op there.
    """
    try:
        serializer = getattr(transport.output()._params, "serializer", None)
        if serializer is not None and hasattr(serializer, "_hangup_attempted"):
            serializer._hangup_attempted = True
            logger.info("[agent_transfer] Suppressed serializer auto-hangup")
    except Exception as e:
        logger.warning(f"[agent_transfer] Could not suppress auto-hangup: {e}")
