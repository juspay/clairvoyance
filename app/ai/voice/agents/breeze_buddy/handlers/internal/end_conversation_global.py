"""
End Conversation Global Function Handler

Thin wrapper around end_conversation that ensures an outcome is always set
before the call is finalized. If a prior function/hook already set an outcome,
it is preserved. Otherwise, the outcome defaults to BUSY.

When the template defines a "reason" property, the LLM provides a reason for
ending the call (e.g., "user said goodbye", "issue resolved") which is stored
in metadata as call_end_reason.
"""

from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation import (
    end_conversation,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import mute_stt
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger

DEFAULT_OUTCOME = "BUSY"


async def end_conversation_global(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """
    End the conversation via the global function path.

    Ensures an outcome is set before delegating to the core end_conversation
    handler. Preserves any outcome already set by a prior function or hook.

    If the LLM provides a "reason" argument (configured via template properties),
    it is stored in metadata as call_end_reason.

    Args:
        context: Handler context with bot state access
        args: LLM function arguments. May contain "reason" if the template
              defines it as a property.

    Returns:
        Result from end_conversation (empty dict)
    """
    # Mute STT immediately so any customer speech during the goodbye
    # does not trigger another LLM turn.  In normal (node-based) mode
    # this is handled by the end_conversation_node's mute_stt pre_action;
    # in direct mode no such node exists, so we do it here.
    await mute_stt(context, {})

    if context.lead:
        if context.lead.metaData is None:
            context.lead.metaData = {}

        # Capture the LLM-provided reason only if call_end_reason is not already
        # set by a prior path (e.g., user_idle_timeout, client_disconnected).
        if "call_end_reason" not in context.lead.metaData:
            reason = args.get("reason", "end_conversation_global_function")
            context.lead.metaData["call_end_reason"] = reason
        else:
            reason = context.lead.metaData["call_end_reason"]

        if context.lead.outcome is None:
            context.lead.outcome = DEFAULT_OUTCOME
            logger.info(
                f"[end_conversation_global] No outcome set for call {context.call_sid}, "
                f"defaulting to '{DEFAULT_OUTCOME}' (reason: {reason})"
            )
        else:
            logger.info(
                f"[end_conversation_global] Preserving existing outcome "
                f"'{context.lead.outcome}' for call {context.call_sid} "
                f"(reason: {reason})"
            )

    return await end_conversation(context, args)
