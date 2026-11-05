"""Gemini/Vertex streamer for the chat driver.

Mirrors ``GoogleLLMService._process_context`` minus the frame layer, plus
the chat-only extras that exist because of documented Gemini/Vertex
behavior: thought-signature capture (see ``signatures``), per-cycle
forced tool choice (mode=ANY), per-call thinking-level override, static-
prefix context caching (see ``prompt_cache``) with a one-shot full-prefix
retry on a dead cache, and universal-list tool-result compaction.

Split out of ``driver.py`` (2026-08-04): the dispatcher and the
OpenAI/Anthropic streamers there are essentially stock mirrors, while
this path carries all of chat's Gemini-specific machinery — the provider
package is where that belongs. ``driver.stream`` remains the only entry
point; nothing imports this module directly except the driver and tests.

Same private-attr posture as the driver (``service._client`` /
``service._settings`` — pipecat 1.1 has no public streaming-with-tools
entry point; revisit on upgrade).
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService

from app.ai.voice.agents.breeze_buddy.chat.history.compactor import (
    compact_tool_results_universal,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.common import (
    DriverEvent,
    close_stream,
)
from app.ai.voice.agents.breeze_buddy.chat.llm.gemini.prompt_cache import (
    invalidate_prompt_cache,
    resolve_prompt_cache,
)
from app.core.logger import logger


async def stream_gemini(
    service: GoogleLLMService,
    context: LLMContext,
    log_label: str,
    tool_context_retention: Optional[Dict[str, str]] = None,
    tool_context_projection: Optional[Dict[str, List[str]]] = None,
    allowed_function_names: Optional[List[str]] = None,
    thinking_level_override: Optional[str] = None,
) -> AsyncIterator[DriverEvent]:
    """Mirror GoogleLLMService._process_context minus frame pushes.

    Gemini chunks are part-shaped, not delta-shaped: each ``part`` is either
    fully-formed text or a fully-materialized ``function_call`` (no
    accumulation needed).

    Thought signatures (thinking enabled) are captured per pipecat's
    reference implementation and yielded as ``("context_message", …)``
    events. Placement mirrors GoogleLLMService._process_context:

    - Gemini 2.5: signatures ride function_call Parts, and then the last
      Part of model responses following the first function_call in a
      conversation.
    - Gemini 3: signatures ride the last Part of a model response
      regardless of Part type — which in streaming means they can arrive
      on a trailing EMPTY-text chunk (``part.text == ""``), so the
      signature check runs outside the text/function_call dispatch and
      the text bookmark uses ALL text accumulated so far this response.

    Each signature carries a "bookmark" (function_call id | text |
    inline_data) that GeminiLLMAdapter._apply_thought_signatures_to_messages
    later uses to re-attach the signature when converting context back to
    Gemini shape. For function calls the bookmark id MUST equal the
    ``tool_call_id`` of the yielded FunctionCallFromLLM — the adapter
    matches ``part.function_call.id`` against it — so when ``fc.id`` is
    None the fallback uuid is generated once and used for both.
    """
    import uuid as _uuid

    from google.genai.types import GenerateContentConfig

    adapter = service.get_llm_adapter()

    # Context compaction (Phase 2 Gemini port): rewrite stale tool-result
    # messages on the UNIVERSAL list, then adapt a shadow context. Runs
    # BEFORE provider adaptation, so thought signatures (LLMSpecificMessage
    # entries — passed through verbatim) and function_call parts keep their
    # exact wire shape; only stale tool-result content shrinks. The live
    # ``context`` is never mutated — the caller owns it.
    call_context = context
    if tool_context_retention:
        compacted = compact_tool_results_universal(
            context.get_messages(),
            retention=tool_context_retention,
            recent_keep=1,
            projection=tool_context_projection,
        )
        # Same private-attr posture as service._client above — pipecat 1.1
        # exposes no public copy-with-messages constructor path.
        call_context = LLMContext(
            messages=compacted,
            tools=context.tools,
            tool_choice=context._tool_choice,
        )

    invocation_params = adapter.get_llm_invocation_params(
        call_context, system_instruction=service._settings.system_instruction
    )

    # Per-cycle forced tool choice (RFC-002): mode=ANY constrains the model
    # to emit one of the named function calls — enforcement by construction
    # at the API layer, not by prompt. Overrides the session-level
    # tool_config for THIS call only.
    tool_config = service._tool_config
    if allowed_function_names:
        tool_config = {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": list(allowed_function_names),
            }
        }

    # Go through the service builder so model-aware thinking defaults apply
    # (e.g. Gemini 2.5 Flash thinking_budget=0).
    generation_params = service._build_generation_params(
        system_instruction=invocation_params["system_instruction"],
        tools=invocation_params["tools"] or [],
        tool_config=tool_config,
    )
    service._maybe_unset_thinking_budget(generation_params)
    if thinking_level_override:
        # Per-call thinking swap (e.g. cycle-1 routing and the final
        # quick-replies cycle run 'minimal'). ONLY when the resolved
        # config already speaks the thinking_level dialect: thinking_level
        # is a Gemini-3-era knob, and sending it to a budget-dialect model
        # (Gemini 2.5 — including the 2.5-flash budget=0 default the
        # service builder applies) is a 400 INVALID_ARGUMENT. Since the
        # cycle-1 override fires on EVERY turn, applying it blindly would
        # brick every 2.5-era template — so budget-dialect configs keep
        # their template-configured thinking untouched and simply skip the
        # optimization.
        thinking_config = dict(generation_params.get("thinking_config") or {})
        if "thinking_level" in thinking_config:
            thinking_config["thinking_level"] = thinking_level_override
            generation_params["thinking_config"] = thinking_config
        else:
            logger.debug(
                f"[{log_label}] llm_driver: thinking_level_override skipped "
                "(budget-dialect thinking config)"
            )

    raw_model_name = service._settings.model
    if not isinstance(raw_model_name, str):
        raise RuntimeError(
            f"chat llm_driver: gemini service has no concrete model name (got {raw_model_name!r})"
        )
    # Narrowed rebind — the `_open_stream` closure below can't see the
    # isinstance guard's narrowing on a captured variable.
    model_name: str = raw_model_name

    logger.debug(f"[{log_label}] llm_driver: gemini stream model={model_name}")

    # Static-prefix cache (prompt_cache.py): unforced cycles swap the
    # system_instruction + tools for a CachedContent reference — the
    # 10k+-token prefix prefills once per TTL instead of per cycle. A
    # request with cached content must NOT carry tools/system/tool_config
    # (API 400), so forced-choice cycles (chips, plan steps — any
    # ``allowed_function_names`` or session tool_config) send the full
    # prefix as before. Resolve failure → full prefix; never a regression.
    full_generation_params = generation_params
    cached_name: Optional[str] = None
    if (
        not allowed_function_names
        and not generation_params.get("tool_config")
        and generation_params.get("system_instruction")
    ):
        cached_name = await resolve_prompt_cache(
            service._client,
            model=model_name,
            system_instruction=generation_params.get("system_instruction"),
            tools=generation_params.get("tools"),
        )
    if cached_name:
        generation_params = {
            k: v
            for k, v in generation_params.items()
            if k not in ("system_instruction", "tools", "tool_config")
        }
        generation_params["cached_content"] = cached_name

    # ``generate_content_stream`` returns a plain async generator
    # (google/genai/models.py: ``base_async_generator``) that wraps the
    # underlying ``AsyncStream``. Closing it via Python's native
    # ``aclose()`` cancels the inner ``async for`` and cascades to the
    # SDK's stream cleanup.
    #
    # The generator is LAZY: the HTTP request fires on the FIRST
    # ``__anext__``, not when the call above returns — so a dead cache
    # reference (server evicted / TTL lapsed while our registry thought
    # it was live) surfaces as a 404 on the first chunk. ``_open_stream``
    # therefore pulls that first chunk eagerly, and the cached path
    # retries ONCE with the full prefix on ANY first-chunk failure.
    # (Live incident 2026-07-31: overnight expiry 404'd five greeting
    # turns because the old fallback only wrapped the lazy call.)
    async def _open_stream(params: Dict[str, Any]):
        stream = await service._client.aio.models.generate_content_stream(
            model=model_name,
            contents=invocation_params["messages"],
            config=GenerateContentConfig(**params),
        )
        iterator = stream.__aiter__()
        try:
            head = await iterator.__anext__()
        except StopAsyncIteration:
            head = None
        return stream, iterator, head

    try:
        response, stream_iter, first_chunk = await _open_stream(generation_params)
    except Exception as exc:
        if cached_name is None:
            raise
        # Cache lapsed or was rejected server-side — drop it and retry
        # once with the full prefix (the pre-cache params, untouched).
        invalidate_prompt_cache(cached_name)
        logger.warning(
            f"[{log_label}] llm_driver: cached-content request failed "
            f"({type(exc).__name__}: {str(exc)[:120]}) — retrying full prefix"
        )
        response, stream_iter, first_chunk = await _open_stream(full_generation_params)

    async def _chunks():
        if first_chunk is not None:
            yield first_chunk
        async for tail_chunk in stream_iter:
            yield tail_chunk

    accumulated_text = ""
    finish_reason: Optional[str] = None
    try:
        async for chunk in _chunks():
            if not chunk.candidates:
                continue
            for candidate in chunk.candidates:
                raw_reason = getattr(candidate, "finish_reason", None)
                if raw_reason is not None:
                    # Enum or str depending on SDK path; normalize to the
                    # bare name ("STOP", "MALFORMED_FUNCTION_CALL", …).
                    finish_reason = getattr(raw_reason, "name", None) or str(raw_reason)
                if not (candidate.content and candidate.content.parts):
                    continue
                for part in candidate.content.parts:
                    function_call_id: Optional[str] = None
                    if part.text and not part.thought:
                        accumulated_text += part.text
                        yield ("text", part.text)
                    elif part.function_call:
                        fc = part.function_call
                        if not fc.name:
                            logger.warning(
                                f"[{log_label}] llm_driver: gemini function_call "
                                "with no name; skipping"
                            )
                            continue
                        # Generated ONCE — the same id must flow into the
                        # assistant message's tool_calls entry AND the
                        # thought-signature bookmark below, or the adapter
                        # can never re-attach the signature.
                        function_call_id = fc.id or str(_uuid.uuid4())
                        yield (
                            "tool_call",
                            FunctionCallFromLLM(
                                function_name=fc.name,
                                tool_call_id=function_call_id,
                                arguments=fc.args or {},
                                context=context,
                            ),
                        )

                    # Thought-signature capture — see the docstring for the
                    # Gemini 2.5 vs 3 placement rules this replicates.
                    if part.thought_signature:
                        bookmark: Dict[str, Any] = {}
                        if part.function_call:
                            if function_call_id is not None:
                                bookmark["function_call"] = function_call_id
                        elif part.inline_data and part.inline_data.data:
                            bookmark["inline_data"] = part.inline_data
                        elif part.text is not None:
                            # Gemini 3 trailing empty-text chunk: bookmark
                            # with all the (non-thought) text seen so far
                            # in this response's chunks.
                            bookmark["text"] = accumulated_text
                        else:
                            logger.warning(
                                f"[{log_label}] llm_driver: thought signature "
                                "found on unhandled Part type"
                            )
                        if bookmark:
                            yield (
                                "context_message",
                                adapter.create_llm_specific_message(
                                    {
                                        "type": "thought_signature",
                                        "signature": part.thought_signature,
                                        "bookmark": bookmark,
                                    }
                                ),
                            )
    finally:
        await close_stream(response)
    if finish_reason is not None and finish_reason != "STOP":
        yield ("finish_reason", finish_reason)


__all__ = ["stream_gemini"]
