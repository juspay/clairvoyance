"""
Built-in Function Dispatcher

Central dispatcher for built-in global functions. Routes to the correct
handler based on function_config.handler field.

To add a new built-in global function:
1. Create a handler in handlers/internal/ with signature: (context, args) -> Dict
2. Import it here and add to BUILTIN_HANDLERS registry
3. Use it in templates with: {"type": "builtin", "handler": "<handler_key>", ...}
"""

import asyncio
from typing import Any, Callable, Dict, Optional, Tuple

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TTSSpeakFrame,
)

from app.ai.voice.agents.breeze_buddy.handlers.internal.agent_transfer import (
    connect_to_agent,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.end_conversation_global import (
    end_conversation_global,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.get_current_time import (
    get_current_time,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.hold_and_consult import (
    hold_and_consult,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.query_knowledge_base import (
    query_knowledge_base,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.send_whatsapp_message import (
    send_whatsapp_message,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.stt import (
    mute_stt,
    unmute_stt,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.update_outcome import (
    update_outcome,
)
from app.ai.voice.agents.breeze_buddy.handlers.internal.warm_transfer import (
    connect_to_live_agent,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import GlobalBuiltinFunction
from app.core.logger import logger

# Registry of built-in handler name -> handler function.
# Each handler has signature: (context: TemplateContext, args: Dict) -> Dict
# To add a new built-in function, import it and add an entry here.
BUILTIN_HANDLERS: Dict[str, Callable] = {
    "connect_to_agent": connect_to_agent,
    "connect_to_live_agent": connect_to_live_agent,
    "end_conversation": end_conversation_global,
    "get_current_time": get_current_time,
    "hold_and_consult": hold_and_consult,
    "query_knowledge_base": query_knowledge_base,
    "send_whatsapp_message": send_whatsapp_message,
    "update_outcome": update_outcome,
}

# Handlers that receive their own function-entry config. Transfer targets/limits
# and WhatsApp value-count constraints live on the function entry, so these
# handlers need function_config forwarded to them. Every other builtin keeps the
# plain (context, args) contract.
_CONFIG_AWARE_HANDLERS = {"connect_to_agent", "send_whatsapp_message"}


async def _speak_and_wait(context: TemplateContext, message: str) -> None:
    """Push a TTS message and wait for it to be fully spoken, uninterrupted.

    Mutes STT/VAD before speaking to prevent user interruptions, then uses a
    two-phase wait (BotStartedSpeaking → BotStoppedSpeaking) to ensure we only
    return after OUR TTS has finished, not a stale BotStoppedSpeakingFrame from
    a previous utterance (e.g., the LLM's own text response).

    This is safe to call alongside ActionManager because:
    - add_event_handler() APPENDS handlers (does not replace)
    - add_reached_downstream_filter() ADDS to the existing filter set
    """
    task = context.task
    bot_started = False
    bot_stopped_event = asyncio.Event()

    async def _on_frame_reached_downstream(task, frame):
        nonlocal bot_started
        if bot_stopped_event.is_set():
            return  # This handler's job is done; avoid stale reactions
        if isinstance(frame, BotStartedSpeakingFrame):
            bot_started = True
        elif isinstance(frame, BotStoppedSpeakingFrame) and bot_started:
            bot_stopped_event.set()

    # Add BotStartedSpeakingFrame to the downstream filter so we receive it.
    # BotStoppedSpeakingFrame is already in the filter (set by ActionManager).
    # add_reached_downstream_filter uses .update() — safe, doesn't replace.
    task.add_reached_downstream_filter((BotStartedSpeakingFrame,))
    task.add_event_handler("on_frame_reached_downstream", _on_frame_reached_downstream)

    # Mute STT/VAD so user speech cannot interrupt pre_tts_message
    await mute_stt(context, {})

    try:
        logger.info(
            f"[builtin_dispatcher] Speaking pre_tts_message: '{message[:50]}...'"
        )
        await task.queue_frame(TTSSpeakFrame(text=message))
        timeout_seconds = 15
        try:
            await asyncio.wait_for(bot_stopped_event.wait(), timeout=timeout_seconds)
            logger.info("[builtin_dispatcher] pre_tts_message finished speaking")
        except asyncio.TimeoutError:
            logger.warning(
                "[builtin_dispatcher] Timeout waiting for pre_tts_message to finish "
                f"speaking after {timeout_seconds} seconds; continuing anyway"
            )
    finally:
        # Always restore STT/VAD, even if something goes wrong
        await unmute_stt(context, {})


async def builtin_function_dispatcher(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalBuiltinFunction] = None,
) -> Tuple[Dict[str, Any], Optional[Any]]:
    """
    Dispatch to the correct built-in handler based on function_config.handler.

    This dispatcher is registered in handler_map and wrapped with with_context.
    It receives function_config from the BuiltinGlobalFunctionAdapter and routes
    to the actual handler.

    Args:
        context: TemplateContext with bot state access
        args: LLM function arguments
        function_config: GlobalBuiltinFunction with handler name

    Returns:
        Tuple of (result_dict, None). The second element is always None — built-in
        handlers stay on the current node. (Agent-to-agent transfer is handled by
        connect_to_agent ending the pipeline task, not by a node transition.)
    """
    if function_config is None:
        logger.error("[builtin_dispatcher] function_config is required but was None")
        return {
            "status": "error",
            "error": "Function configuration not provided",
        }, None

    handler_name = function_config.handler
    handler = BUILTIN_HANDLERS.get(handler_name)

    if handler is None:
        logger.error(
            f"[builtin_dispatcher] Unknown handler '{handler_name}'. "
            f"Available: {list(BUILTIN_HANDLERS.keys())}"
        )
        return {
            "status": "error",
            "error": f"Unknown built-in handler: {handler_name}",
        }, None

    logger.info(
        f"[builtin_dispatcher] Dispatching '{function_config.name}' -> '{handler_name}'"
    )

    try:
        # If pre_tts_message is configured, speak it and wait for TTS completion
        # before executing the handler. This guarantees the message is heard even
        # if the handler terminates the pipeline (e.g., connect_to_live_agent
        # pushes EndFrame which kills TTS mid-sentence).
        if function_config.pre_tts_message:
            await _speak_and_wait(context, function_config.pre_tts_message)

        if handler_name in _CONFIG_AWARE_HANDLERS:
            result = await handler(context, args, function_config)
        else:
            result = await handler(context, args)
        # Ensure consistent return format (result, None) for global functions
        if isinstance(result, tuple):
            result_dict, next_node = result
        else:
            result_dict, next_node = result, None

        # Record in node traversal path with the actual function response.
        # This allows downstream logic (including the LLM via conversation
        # context) to inspect the result of built-in calls such as
        # transfer_to_agent for auditing and decision-making.
        context.record_global_function_call(
            function_name=function_config.name,
            function_args=args,
            function_response=result_dict,
        )

        return result_dict, next_node
    except Exception as e:
        logger.error(
            f"[builtin_dispatcher] Error in handler '{handler_name}': {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "error": str(e),
        }, None
