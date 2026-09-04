"""Turn-1 prompt-cache prefill for Azure/OpenAI voice templates.

The provider's automatic prompt cache (exact-token-prefix match, >=1024
tokens, 5-10 min TTL) is always cold at a call's first LLM inference; turns
2+ hit it for free. When ``llm_configurations.prefill_system_prompt`` is set,
this module fires ONE cheap ``chat.completions`` request (non-streaming,
``max_completion_tokens=16``) carrying the exact rendered system prefix + the
exact tools array, fire-and-forget, in the gap between flow init and the
first inference — for standard outbound telephony that gap is the greeting
playback (~3-6s), which is the whole point: turn 1 then reads a warm cache.

Prefix parity is the entire contract, and it holds BY CONSTRUCTION: params
are assembled through the same pipecat methods the real request uses
(``get_llm_adapter().get_llm_invocation_params`` → ``build_chat_completion_params``),
so ``service_tier``, ``settings.extra`` (reasoning_effort), developer-role
handling, and the ``no tools ⇒ no tools key`` rule cannot drift. The failure
mode of any mismatch is only a cache miss (visible as turn-1
``cache_read`` = 0 in the metrics), never a correctness issue.

Scope: Azure/OpenAI text-LLM services only (Gemini chat caches explicitly
via chat/llm/gemini/prompt_cache.py; Vertex Claude via
``enable_prompt_caching``). The flag is deliberately not validated at
template save — on any other provider it is simply inert, and the gate in
:func:`spawn_prefill` is the single enforcement point: it skips with a
per-call INFO so an inert-but-flagged config is visible in call logs.
Personalized-per-lead prompts mean the cache can never hit across calls —
this is strictly a within-call turn-1 play.

Private-attr posture: reads ``service._client`` / ``service._settings`` like
the chat driver does (pipecat 1.1 exposes no public non-streaming entry point
that returns usage — ``run_inference`` discards it). Verified against
pipecat 1.1.0 + pipecat-ai-flows 1.1.0; revisit on upgrade.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, cast

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import (
    NOT_GIVEN,
    LLMContext,
    LLMContextMessage,
)
from pipecat.services.openai.base_llm import BaseOpenAILLMService
from pipecat_flows.types import FlowsDirectFunctionWrapper, FlowsFunctionSchema

from app.ai.voice.agents.breeze_buddy.utils.common import track_error
from app.ai.voice.llm.types import LLMConfiguration, LLMProvider
from app.core.concurrency import spawn_background_task
from app.core.logger import logger

# Decoding params are not part of the provider's cache key, so the completion
# budget is free to differ from the real request. 16 (not 1) so a template
# with thinking.enabled (reasoning_effort in settings.extra) can't 400 on a
# budget the reasoning alone would exhaust.
_PREFILL_MAX_COMPLETION_TOKENS = 16
_PREFILL_TIMEOUT_SECS = 8.0

__all__ = ["spawn_prefill"]


def spawn_prefill(
    *,
    llm_service: Any,
    llm_config: Optional[LLMConfiguration],
    initial_node_config: Any,  # pipecat_flows NodeConfig (a TypedDict)
    global_functions: List[Any],
    errors: Optional[List[Dict[str, Any]]] = None,
) -> Optional[asyncio.Task]:
    """Spawn the turn-1 cache prefill if the template opted in, else no-op.

    Sync and non-blocking: returns the spawned task (tests may await it;
    production callers ignore it) or ``None`` when gated off. Never raises.
    """
    if llm_config is None or not llm_config.prefill_system_prompt:
        return None  # steady state for every unflagged template — stay quiet

    skip: Optional[str] = None
    if llm_config.realtime is not None:
        skip = "realtime LLM has no chat.completions prefix to warm"
    elif llm_config.provider not in (None, LLMProvider.AZURE, LLMProvider.OPENAI):
        # .value is absent when a hand-forced config carries a raw string
        provider = getattr(llm_config.provider, "value", llm_config.provider)
        skip = f"provider '{provider}' has no automatic prefix cache"
    elif not isinstance(llm_service, BaseOpenAILLMService):
        skip = (
            f"service {type(llm_service).__name__} is not an Azure/OpenAI "
            "text-LLM service"
        )
    if skip is not None:
        logger.info(f"prefill: skipped ({skip})")
        return None

    messages = list(initial_node_config.get("role_messages") or []) + list(
        initial_node_config.get("task_messages") or []
    )
    if not messages:
        return None

    return spawn_background_task(
        prefill_system_prompt(
            llm_service=llm_service,
            messages=messages,
            function_entries=_function_entries(
                global_functions, initial_node_config.get("functions") or []
            ),
            errors=errors,
        ),
        name="prompt-cache-prefill",
    )


async def prefill_system_prompt(
    *,
    llm_service: Any,
    messages: List[Dict[str, Any]],
    function_entries: List[Any],
    errors: Optional[List[Dict[str, Any]]],
) -> None:
    """Fire the parity request and log the outcome. All failures are logged
    + tracked, never re-raised: a broken prefill must not be able to affect
    the call it is warming.

    ``messages`` is role_messages + task_messages in pipecat-flows'
    serialization order, deliberately with NO trailing user message: the list
    is the longest common prefix of both first-request shapes (greeting
    played → ``+ user transcript``; respond_immediately → exactly this list),
    so appending anything would only add a divergence point and cost.
    """
    started = time.monotonic()
    try:
        adapter = llm_service.get_llm_adapter()
        # Same conversion pipecat_flows' adapter.format_functions performs
        # (to_function_schema → ToolsSchema; empty → NOT_GIVEN, which the LLM
        # adapter and the OpenAI SDK both collapse to "no tools key") —
        # format_functions itself lives on the pipecat_flows adapter, not the
        # service's, so the step is inlined here.
        standard_functions = [entry.to_function_schema() for entry in function_entries]
        tools = (
            ToolsSchema(standard_tools=standard_functions)
            if standard_functions
            else NOT_GIVEN
        )
        context = LLMContext(
            # pipecat-flows stores plain message dicts; they are a valid
            # LLMContextMessage (LLMStandardMessage), just not in pyrefly's eyes
            messages=cast(List[LLMContextMessage], messages),
            tools=tools,
        )
        invocation = adapter.get_llm_invocation_params(
            context,
            system_instruction=llm_service._settings.system_instruction,
            convert_developer_to_user=not llm_service.supports_developer_role,
        )
        params = llm_service.build_chat_completion_params(invocation)
        # Non-streaming one-shot; drop the streaming-only usage option and the
        # settings' completion budget in favor of the prefill's own (above).
        params["stream"] = False
        params.pop("stream_options", None)
        params.pop("max_tokens", None)
        params["max_completion_tokens"] = _PREFILL_MAX_COMPLETION_TOKENS

        response = await asyncio.wait_for(
            llm_service._client.chat.completions.create(**params),
            timeout=_PREFILL_TIMEOUT_SECS,
        )
    except Exception as exc:  # noqa: BLE001 — prefill is best-effort by design
        logger.opt(exception=exc).warning(
            f"prefill: failed ({type(exc).__name__}: {str(exc)[:160]}) — "
            "call proceeds on a cold cache"
        )
        track_error(errors, f"prompt prefill failed: {exc}")
        return

    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    # Newer Azure model families (GPT-5.6+) can bill cache writes separately
    # from discounted reads — surface the write count when the provider
    # reports it (absent on gpt-4.1/4o today).
    cache_write = getattr(details, "cache_write_tokens", None)
    write_note = f" cache_write={cache_write}" if cache_write is not None else ""
    logger.info(
        f"prefill: warmed model={params['model']} "
        f"ms={round((time.monotonic() - started) * 1000)} "
        f"prompt={getattr(usage, 'prompt_tokens', '?')} "
        f"cached={getattr(details, 'cached_tokens', None)}{write_note}"
    )


def _function_entries(
    global_functions: List[Any], node_functions: List[Any]
) -> List[Any]:
    """Mirror pipecat_flows FlowManager._set_node's function registration
    (manager.py:595-650): globals FIRST, then node functions; callables wrap
    as FlowsDirectFunctionWrapper, FlowsFunctionSchema pass through. Anything
    else is dropped with a warning — the manager would raise, but a prefill
    must never break a call; dropping one just guarantees a tools mismatch
    (i.e. a cache miss) for this call.
    """
    entries: List[Any] = []
    for func in list(global_functions or []) + list(node_functions or []):
        if callable(func):
            entries.append(FlowsDirectFunctionWrapper(function=func))
        elif isinstance(func, FlowsFunctionSchema):
            entries.append(func)
        else:
            logger.warning(
                f"prefill: dropping unsupported function entry "
                f"({type(func).__name__}) — tools may mismatch, cache will miss"
            )
    return entries
