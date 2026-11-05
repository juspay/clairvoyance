"""Direct LLM streaming + tool-call accumulator for chat mode.

One streaming SDK call per invocation; yields ``("text", str)`` for content
deltas and ``("tool_call", FunctionCallFromLLM)`` after the stream closes for
each materialized tool call. Provider dispatch is by ``isinstance`` against
pipecat's three base classes — covers every provider in pipecat 1.1's
``services/`` tree (current Buddy usage: Azure, Vertex Gemini, Vertex Claude).

The Gemini streamer lives in ``gemini/stream.py`` (all of chat's
Gemini-specific machinery rides there); this module keeps the dispatcher
plus the stock-mirror OpenAI and Anthropic streamers.

The Gemini path additionally yields ``("context_message", LLMSpecificMessage)``
for thought signatures (Gemini 2.5/3 thinking). The driver never mutates
``context`` — the caller owns it and must append these messages itself so
the signatures are echoed back on the next request (Vertex 400s a
``functionCall`` part that comes back without its ``thought_signature``).

We read ``service._client`` and ``service._settings`` directly: pipecat 1.1
exposes no public streaming-with-tools entry point. Tested with pipecat 1.1.0
+ pipecat-ai-flows 1.1.0; revisit on upgrade.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.base_llm import BaseOpenAILLMService

from app.ai.voice.agents.breeze_buddy.chat.history.compactor import (
    compact_tool_results,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.common import (
    DriverEvent,
    close_stream,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.stream import stream_gemini
from app.core.logger import logger

__all__ = ["DriverEvent", "stream"]


async def stream(
    llm_service: Any,
    context: LLMContext,
    *,
    log_label: str = "chat",
    tool_context_retention: Optional[Dict[str, str]] = None,
    tool_context_projection: Optional[Dict[str, List[str]]] = None,
    allowed_function_names: Optional[List[str]] = None,
    thinking_level_override: Optional[str] = None,
) -> AsyncIterator[DriverEvent]:
    """Issue one streaming LLM call; yield text deltas + tool calls.

    The caller owns ``context`` mutation between turns of the tool-call loop.
    This function only reads it.

    ``allowed_function_names`` constrains THIS call to the named functions
    (forced tool choice — RFC-002 forced-think + plan enforcement). Gemini:
    ``function_calling_config`` mode=ANY + allowed_function_names — the
    model MUST emit one of the named calls. OpenAI/Anthropic paths do not
    implement it yet (chat runs Gemini; a set value raises loudly rather
    than silently un-enforcing).

    The Gemini path also yields ``("finish_reason", str)`` after the stream
    closes when the last candidate carried a finish reason — the caller's
    MALFORMED_FUNCTION_CALL fallback (retry once unforced) keys off it.

    ``tool_context_retention`` is an optional per-tool policy map (honoured
    by the Anthropic AND Gemini paths) that lets the compactor rewrite
    stale ``tool_result`` blocks — bounding input-token cost across long
    sessions. ``None`` or an empty map is a no-op.

    ``tool_context_projection`` is the companion per-tool keep-list map: when a
    ``last_turn_only`` tool has an entry, its stale results are compacted to an
    identity projection (whitelisted paths only) instead of a bare stub, so the
    LLM keeps durable referents (product url/handle/price, variant ids) at a
    fraction of the tokens. Anthropic compacts the provider-shape messages;
    Gemini compacts the universal list pre-adaptation (thought signatures
    and function_call parts are untouched either way).

    ``thinking_level_override`` swaps the thinking level for THIS call only
    (Gemini path; e.g. the forced final quick-replies cycle runs 'low' — a
    trivial generation that shouldn't pay medium-thinking latency).
    """
    if isinstance(llm_service, GoogleLLMService):
        async for event in stream_gemini(
            llm_service,
            context,
            log_label,
            tool_context_retention,
            tool_context_projection,
            allowed_function_names,
            thinking_level_override,
        ):
            yield event
        return
    if allowed_function_names:
        # Forcing is a RELIABILITY guarantee — degrading it silently would
        # reintroduce the exact failure class it exists to prevent.
        raise NotImplementedError(
            "allowed_function_names forcing is implemented for the Gemini "
            f"path only (got {type(llm_service).__name__})"
        )
    if isinstance(llm_service, BaseOpenAILLMService):
        async for event in _stream_openai(llm_service, context, log_label):
            yield event
        return
    if isinstance(llm_service, AnthropicLLMService):
        async for event in _stream_anthropic(
            llm_service,
            context,
            log_label,
            tool_context_retention,
            tool_context_projection,
        ):
            yield event
        return

    raise TypeError(
        f"chat llm_driver: unsupported LLM service type {type(llm_service).__name__}"
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible (Azure, OpenAI direct, DeepSeek, Groq, Cerebras, …)
# ---------------------------------------------------------------------------


async def _stream_openai(
    service: BaseOpenAILLMService,
    context: LLMContext,
    log_label: str,
) -> AsyncIterator[DriverEvent]:
    """Mirror BaseOpenAILLMService._process_context minus the frame layer.

    Tool-call deltas arrive interleaved with text deltas, indexed by
    ``tool_calls[*].index``. Per-index we accumulate name + args fragments
    and flush on index advance / stream close.
    """
    adapter = service.get_llm_adapter()
    invocation_params = adapter.get_llm_invocation_params(
        context,
        system_instruction=service._settings.system_instruction,
        convert_developer_to_user=not service.supports_developer_role,
    )

    settings = service._settings
    params: dict[str, Any] = {
        "model": settings.model,
        "stream": True,
        "messages": invocation_params["messages"],
        "tools": invocation_params["tools"],
        "tool_choice": invocation_params["tool_choice"],
    }
    # Forward only explicitly-set settings so we don't paint over provider
    # defaults with NOT_GIVEN sentinels.
    for attr in (
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "max_tokens",
        "max_completion_tokens",
    ):
        value = getattr(settings, attr, None)
        if value is not None:
            params[attr] = value
    params.update(getattr(settings, "extra", None) or {})

    logger.debug(f"[{log_label}] llm_driver: openai stream model={settings.model}")

    # Per-index aggregation: OpenAI's streaming spec emits ``delta.tool_calls``
    # as an *array* of fragments, each tagged with a sparse ``index``. Multiple
    # fragments — sometimes for *different* indices — can ride the same chunk
    # when the model invokes parallel tool calls. Reading only ``[0]`` and
    # advancing a sequential counter drops any second-or-later entry in the
    # chunk and breaks the moment indices arrive non-monotonically.
    tool_acc: dict[int, dict[str, str]] = {}

    stream_obj = await service._client.chat.completions.create(**params)
    try:
        async for chunk in stream_obj:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = tool_acc.setdefault(
                        tc.index, {"name": "", "args": "", "id": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
            elif delta.content:
                yield ("text", delta.content)
    finally:
        if hasattr(stream_obj, "close"):
            try:
                await stream_obj.close()
            except Exception:
                pass

    for idx in sorted(tool_acc.keys()):
        slot = tool_acc[idx]
        name = slot["name"]
        raw_args = slot["args"]
        tid = slot["id"]
        if not name:
            continue
        try:
            parsed_args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            # Don't dump raw_args — tool payloads can carry merchant/user data.
            logger.warning(
                f"[{log_label}] llm_driver: bad tool args for {name!r} "
                f"(payload_length={len(raw_args)})"
            )
            parsed_args = {}
        yield (
            "tool_call",
            FunctionCallFromLLM(
                function_name=name,
                tool_call_id=tid,
                arguments=parsed_args,
                context=context,
            ),
        )


# ---------------------------------------------------------------------------
# Anthropic (Claude on Vertex, Anthropic direct)
# ---------------------------------------------------------------------------


async def _stream_anthropic(
    service: AnthropicLLMService,
    context: LLMContext,
    log_label: str,
    tool_context_retention: Optional[Dict[str, str]] = None,
    tool_context_projection: Optional[Dict[str, List[str]]] = None,
) -> AsyncIterator[DriverEvent]:
    """Mirror AnthropicLLMService._process_context minus frame pushes.

    Anthropic streams discrete events: ``content_block_start(tool_use)`` opens
    a tool call, ``content_block_delta(partial_json=...)`` accumulates args,
    ``message_delta(stop_reason="tool_use")`` finalises it.
    """
    # VertexAnthropicLLMService overrides this hook to append a dummy user
    # turn when context ends on assistant — go through the service so the
    # override kicks in.
    invocation_params = service._get_llm_invocation_params(context)

    settings = service._settings
    params: dict[str, Any] = {
        "model": settings.model,
        "stream": True,
        "max_tokens": settings.max_tokens,
        # Pipecat sends this beta unconditionally; preserve voice parity.
        "betas": ["interleaved-thinking-2025-05-14"],
    }
    for attr in ("temperature", "top_k", "top_p"):
        value = getattr(settings, attr, None)
        if value is not None:
            params[attr] = value
    thinking = getattr(settings, "thinking", None)
    # ``thinking`` is typed as ``ThinkingConfig | NotGiven``; guard the
    # NOT_GIVEN sentinel which has no ``model_dump``.
    if thinking and hasattr(thinking, "model_dump"):
        params["thinking"] = thinking.model_dump(exclude_unset=True)
    params.update(invocation_params)
    params.update(getattr(settings, "extra", None) or {})

    # Compact stale tool_result blocks per the template's retention policy.
    # Applied AFTER invocation_params/settings.extra are merged in, so the
    # compaction reflects exactly what's about to go on the wire. The most
    # recent tool_result stays intact (``recent_keep=1``) so the LLM can
    # reason about the call its current turn just made.
    if tool_context_retention and isinstance(params.get("messages"), list):
        params["messages"] = compact_tool_results(
            params["messages"],
            retention=tool_context_retention,
            recent_keep=1,
            projection=tool_context_projection,
        )

    logger.debug(f"[{log_label}] llm_driver: anthropic stream model={settings.model}")

    cur_tool_block: Any = None
    cur_args = ""
    pending_calls: list[FunctionCallFromLLM] = []

    # The Anthropic SDK returns ``AsyncStream`` here
    # (anthropic/_streaming.py: AsyncStream), which exposes ``close()``.
    # Wrap iteration so an outer cancellation (SSE client disconnect)
    # closes the underlying httpx response instead of waiting for GC.
    response = await service._client.beta.messages.create(**params)
    try:
        async for event in response:
            et = getattr(event, "type", None)
            if et == "content_block_start":
                block = event.content_block
                if block.type == "tool_use":
                    if cur_tool_block is not None:
                        pending_calls.append(
                            _finalize_anthropic_call(
                                cur_tool_block, cur_args, context, log_label
                            )
                        )
                    cur_tool_block = block
                    cur_args = ""
            elif et == "content_block_delta":
                d = event.delta
                if hasattr(d, "text") and d.text:
                    yield ("text", d.text)
                elif hasattr(d, "partial_json") and cur_tool_block is not None:
                    cur_args += d.partial_json or ""
            elif et == "message_delta":
                stop = getattr(getattr(event, "delta", None), "stop_reason", None)
                if stop == "tool_use" and cur_tool_block is not None:
                    pending_calls.append(
                        _finalize_anthropic_call(
                            cur_tool_block, cur_args, context, log_label
                        )
                    )
                    cur_tool_block = None
                    cur_args = ""
    finally:
        await close_stream(response)

    # Flush any block dangling at stream end.
    if cur_tool_block is not None:
        pending_calls.append(
            _finalize_anthropic_call(cur_tool_block, cur_args, context, log_label)
        )

    for call in pending_calls:
        yield ("tool_call", call)


def _finalize_anthropic_call(
    block: Any, raw_args: str, context: LLMContext, log_label: str
) -> FunctionCallFromLLM:
    try:
        parsed_args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        # Don't dump raw_args — tool payloads can carry merchant/user data.
        logger.warning(
            f"[{log_label}] llm_driver: bad Anthropic tool args for "
            f"{block.name!r} (payload_length={len(raw_args)})"
        )
        parsed_args = {}
    return FunctionCallFromLLM(
        function_name=block.name,
        tool_call_id=block.id,
        arguments=parsed_args,
        context=context,
    )
