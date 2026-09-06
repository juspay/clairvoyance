"""
Unified Transition Handler

This handler replaces all individual handlers. It:
1. Immediately transitions to the next node (synchronous)
2. Triggers hooks asynchronously (fire and forget)
"""

import asyncio
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.observability.tracing_setup import auto_trace
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.hooks import HookRegistry
from app.ai.voice.agents.breeze_buddy.template.input_collection import (
    get_node_user_speech_timeout,
)
from app.ai.voice.agents.breeze_buddy.template.interruption import (
    apply_node_interruption_config,
    reset_interruption_to_default,
)
from app.ai.voice.agents.breeze_buddy.template.tool_speech import (
    SENTENCE_QUEUE_GAP_SECS,
    queue_say_sentences,
    render_say,
)
from app.ai.voice.agents.breeze_buddy.template.types import HookConfig, SayConfig
from app.ai.voice.agents.breeze_buddy.template.vad import (
    apply_node_vad_config,
    reset_vad_to_default,
)
from app.core.logger import logger


async def _speak_say_block(
    context: TemplateContext,
    say: Dict[str, Any],
    args: Dict[str, Any],
    function_name: str,
) -> None:
    """Render and queue the speech side effect for a tool_based function.

    The TTSSpeakFrame enters the pipeline at the top (via task.queue_frame),
    flows through TTS, and — with ``append_to_context=True`` — is committed
    to the LLM context as the assistant's message for this turn, so the
    model knows what was said without ever having written it.
    """
    from app.ai.voice.agents.breeze_buddy.handlers.internal import end_conversation

    say_config = SayConfig.model_validate(say)
    template_vars = getattr(context.bot, "template_vars", None) or {}
    text, language = render_say(say_config, args, template_vars)

    if context.task is None:
        logger.error(
            f"[{function_name}] say: no pipeline task; dropping speech {text[:80]!r}"
        )
        return

    # Early-speech dedup: the utterance may already be queued — the
    # early-speech router fires TTS on the function-name decode, ahead of
    # argument completion. Consuming the one-shot marker keeps hooks,
    # end_call and node transitions running exactly as before.
    router = getattr(context.bot, "early_speech_router", None)
    spoken_early = (
        router.consume_pending(function_name) if router is not None else False
    )

    logger.info(
        f"[{function_name}] say ({language}){' [early]' if spoken_early else ''}: "
        f"{text[:120]!r}"
        + (f" [+ {len(text) - 120} more chars]" if len(text) > 120 else "")
    )
    if not spoken_early:
        parts = await queue_say_sentences(context.task, text)
        if parts > 1:
            logger.info(
                f"[{function_name}] say split into {parts} sentences for TTS "
                f"(gap {SENTENCE_QUEUE_GAP_SECS * 1000:.0f}ms)"
            )

    if say_config.end_call:
        logger.info(
            f"[{function_name}] say.end_call set: running end_conversation "
            f"after goodbye speech"
        )
        await end_conversation(context, args)


@auto_trace("transition_handler")
async def transition_handler(
    context: TemplateContext,
    args: Dict[str, Any],
    transition_to: Optional[str] = None,
    hooks: Optional[List[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
    say: Optional[Dict[str, Any]] = None,
):
    """
    Unified handler for all workflow transitions.

    This handler:
    1. Renders + queues the ``say`` speech block (tool_based mode) BEFORE any
       node churn so the utterance is ordered ahead of context updates
    2. Immediately transitions to the next node (if specified)
    3. Executes hooks asynchronously without blocking
    4. Handles VAD parameter reset and node-specific VAD configuration

    Args:
        context: Handler context with bot state access
        args: Function arguments from LLM
        transition_to: Target node to transition to
        hooks: List of hook configuration dictionaries (serialized HookConfig objects)
        function_name: Name of the function that was called
        say: Serialized SayConfig (tool_based mode speech side effect). When
            its ``end_call`` flag is set, the goodbye is spoken and the call
            finalizes — no transition is needed or performed.

    Returns:
        Tuple of (result_dict, next_node_config) for immediate transition
    """
    logger.info(
        f"Transition handler called - function: '{function_name}', "
        f"transition_to: '{transition_to}', hooks: {hooks}, "
        f"say: {'yes' if say else 'no'}, args: {args}"
    )

    # tool_based speech side effect: the tool talks, not the model. Runs
    # first so the TTSSpeakFrame is queued before any node transition
    # rewrites the LLM context.
    if say:
        await _speak_say_block(context, say, args, function_name or "unknown")

    # Execute hooks synchronously (awaited) or asynchronously (fire and forget).
    # This MUST run before the end_call early-return below: outcome tools pair
    # a goodbye (say.end_call) with an update_outcome_in_database hook, and
    # returning on end_call alone dropped the outcome write — the lead finished
    # with outcome=None.
    if hooks:
        awaited = hooks[0].get("awaited", False)
        if awaited:
            logger.info(
                f"Executing {len(hooks)} hook(s) synchronously (awaited) for function '{function_name}'"
            )
            await _execute_hooks_async(context, args, hooks, function_name or "unknown")
        else:
            logger.info(
                f"Scheduling {len(hooks)} hook(s) to execute asynchronously for function '{function_name}'"
            )
            asyncio.create_task(
                _execute_hooks_async(context, args, hooks, function_name or "unknown")
            )
    else:
        logger.debug(f"No hooks to execute for function '{function_name}'")

    if say and SayConfig.model_validate(say).end_call:
        # end_conversation already ran inside _speak_say_block (goodbye +
        # full finalization + EndFrame). There is no "next" for this call.
        return {}, None

    # Handle immediate node transition
    if transition_to:
        logger.info(
            f"Transitioning from current node to '{transition_to}' for function '{function_name}'"
        )

        # Record exit from current node (just mark as exited, no via_function)
        context.record_node_exit()

        # Reset VAD params to default before applying node-specific config
        reset_vad_to_default(context)

        # Get node-specific VAD config and apply it
        apply_node_vad_config(context, transition_to)

        # Determine user_speech_timeout from input collection config BEFORE reset.
        # This is passed to both reset and apply so there's never a window where
        # timeout=0.0 is active while transcripts could arrive and trigger an
        # immediate turn end (race condition fix).
        user_speech_timeout = get_node_user_speech_timeout(context, transition_to)

        # Reset interruption strategies to default (with target timeout already set)
        await reset_interruption_to_default(
            context, user_speech_timeout=user_speech_timeout
        )

        # Get node-specific interruption config and apply it (with same timeout)
        await apply_node_interruption_config(
            context, transition_to, user_speech_timeout=user_speech_timeout
        )

        next_node = context.create_node_from_template(transition_to)

        if next_node is None:
            logger.warning(
                f"Node '{transition_to}' not found; skipping traversal recording for function '{function_name}'"
            )
            return {}, None

        # Record entry into new node (pass the function that brought us here)
        context.record_node_entry(transition_to, function_name, args)

        return {}, next_node
    else:
        logger.info(
            f"No transition specified for function '{function_name}', staying in current node"
        )

        if say:
            # tool_based: the say block already spoke this turn and its text is
            # committed to the LLM context. A truthy result would make pipecat
            # re-run inference immediately (response aggregator's
            # "result requires run_llm" path); with tool_choice=required that
            # forces another tool call — the bot repeating itself forever.
            # Empty result = speak once, then wait for the user.
            return {}, None

        result_message = {
            "result": f"Successfully executed {function_name}",
            "status": "success",
        }
        return result_message, None


async def _execute_hooks_async(
    context: TemplateContext,
    args: Dict[str, Any],
    hook_configs: List[Dict[str, Any]],
    function_name: str,
) -> None:
    """
    Execute hooks asynchronously.

    This function runs in the background and doesn't block the main workflow.

    Args:
        context: Handler context with bot state access
        args: Function arguments from LLM
        hook_configs: List of hook configuration dictionaries (serialized HookConfig objects)
        function_name: Name of the function that triggered these hooks
    """
    logger.info(
        f"Starting async execution of {len(hook_configs)} hook(s) for function '{function_name}'"
    )

    for hook_config_dict in hook_configs:
        # Convert dict back to HookConfig object
        hook_config = HookConfig.model_validate(hook_config_dict)
        logger.debug(
            f"Attempting to execute hook '{hook_config.name}' with expected_fields: {hook_config.expected_fields} "
            f"for function '{function_name}'"
        )
        hook = HookRegistry.get(hook_config.name)

        if hook:
            logger.info(
                f"Executing hook '{hook_config.name}' for function '{function_name}'"
            )
            await hook.safe_execute(context, args, function_name, hook_config)
        else:
            logger.warning(
                f"Hook '{hook_config.name}' not found in registry for function '{function_name}'"
            )

    logger.info(
        f"Completed async execution of {len(hook_configs)} hook(s) for function '{function_name}'"
    )
