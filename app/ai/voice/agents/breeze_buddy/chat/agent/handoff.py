"""handoff_to_human — a chat agent asks for a person (inbox D34).

Offered automatically, never declared by a template: only on a session bound
to an inbox thread (a messaging-channel chat) whose binding has human
handoff switched on. Calling it opens the thread's handoff — the chat lands in the
Inbox's "Needs attention" and Buddy goes silent — and its result ENDS the
turn with the waiting message, so Buddy adds nothing after handing off.

If the handoff cannot be opened (switched off since the turn began, the
thread resolved), the model is told no one is available and keeps helping.
"""

from typing import TYPE_CHECKING, Any, Dict, List

from pipecat_flows import FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.chat.agent.runtime import TURN_END_REPLY_KEY
from app.core.logger import logger
from app.crm.conversations.contracts import (
    HANDOFF_PRIORITIES,
    handoff_available,
    request_handoff,
)

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.chat.agent.core import ChatAgent

HANDOFF_TOOL_NAME = "handoff_to_human"

#: What the customer reads when Buddy hands off (D34: default wording).
WAITING_MESSAGE = "I'm connecting you with our team. Someone will reply here shortly."

REASONS = ["customer_requested", "cannot_resolve", "policy", "sensitive"]


def build_handoff_schema(handler: Any) -> FlowsFunctionSchema:
    return FlowsFunctionSchema(
        name=HANDOFF_TOOL_NAME,
        description=(
            "Hand this conversation to a person on the merchant's team. Call "
            "it when the customer asks for a human, when you cannot resolve "
            "their request, or when it needs a person (policy, sensitive). "
            "After calling it, say nothing more: the customer is told a "
            "teammate will reply."
        ),
        properties={
            "reason": {"type": "string", "enum": REASONS},
            "summary": {
                "type": "string",
                "description": "One or two sentences for the teammate: what "
                "the customer needs and what you already tried.",
            },
            "priority": {"type": "string", "enum": list(HANDOFF_PRIORITIES)},
        },
        required=["reason", "summary"],
        handler=handler,
    )


class HandoffMixin:
    async def _handoff_tools(self: "ChatAgent") -> List[FlowsFunctionSchema]:
        """[the schema] when this session may hand off, else []. Fail
        closed: a lookup that errors offers nothing."""
        if not self.conversation_id or not self.merchant_id:
            return []
        try:
            offered = await handoff_available(
                self.merchant_id, self.conversation_id, self.session_id
            )
        except Exception:  # noqa: BLE001 — never fail a turn over an offer
            logger.exception(f"ChatAgent {self.session_id}: handoff lookup failed")
            return []
        return [build_handoff_schema(self._handoff_handler)] if offered else []

    async def _handoff_handler(
        self: "ChatAgent", args: Dict[str, Any], _flow_manager: Any = None
    ) -> Dict[str, Any]:
        args = args if isinstance(args, dict) else {}
        reason = args.get("reason") if args.get("reason") in REASONS else REASONS[0]
        # Any other word is asked as normal (conversations owns the words).
        priority = str(args.get("priority") or "")
        summary = str(args.get("summary") or "")[:500] or None
        if not self.conversation_id or not self.merchant_id:
            return {"status": "error", "error": "this chat cannot be handed off"}
        try:
            handoff = await request_handoff(
                self.merchant_id,
                self.conversation_id,
                self.session_id,
                reason,
                summary,
                priority,
            )
        except Exception as e:  # noqa: BLE001 — the model must keep helping
            logger.warning(f"ChatAgent {self.session_id}: handoff refused: {e}")
            return {
                "status": "error",
                "error": "No one from the team is available right now. Keep "
                "helping the customer yourself.",
            }
        logger.bind(merchant_id=self.merchant_id, thread_id=self.conversation_id).info(
            f"ChatAgent {self.session_id}: handed off ({reason})"
        )
        return {
            "status": "ok",
            "handoff_id": handoff.id,
            TURN_END_REPLY_KEY: WAITING_MESSAGE,
        }
