"""Voice-side context compaction for MCP tool results.

Chat compacts the large Shopify UCP catalog results via
``compact_tool_results_universal`` (``chat/history/compactor.py``), which
operates on ``role:"tool"`` messages. The voice pipeline stores MCP results
differently: our MCP functions run with ``cancel_on_interruption=False``, so
pipecat treats them as *async* tools and records the result on a
``role:"developer"`` message inside an ``async_tool`` JSON envelope (see
pipecat's ``LLMAssistantAggregator._handle_function_call_finished``). That
envelope is invisible to the chat compactor, so voice needs its own wrapper
that reads the envelope, resolves the tool name via the matching assistant
``tool_call``, and rewrites only the inner ``result`` to a slim projection or
stub. This reuses the chat compactor's projection/stub helpers so behavior
stays consistent across both pipelines.
"""

import json
from typing import Any, Dict, List, Optional

from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)

from app.ai.voice.agents.breeze_buddy.chat.history.compactor import (
    _DEFAULT_RETENTION,
    _project_content,
    _stub_for,
)
from app.core.logger import logger


def build_tool_context_maps(
    configurations: Optional[Any],
) -> tuple[Dict[str, str], Dict[str, List[str]], int]:
    """Extract per-tool retention/projection policies from a template's MCP servers.

    Mirrors the chat path (``chat/agent/tooling.py``): every server's
    ``tool_context_retention`` / ``tool_context_projection`` maps are merged.
    Templates that don't set the fields return empty maps, which makes the
    compactor a no-op — so compaction is opt-in per template.

    Also merges ``bypass_compaction_for_turns`` across servers by taking the
    max — one server opting into a grace window shouldn't be zeroed out by
    another server that left the field at its default.
    """
    retention: Dict[str, str] = {}
    projection: Dict[str, List[str]] = {}
    recent_keep = 0
    mcp_config = getattr(configurations, "mcp", None) if configurations else None
    servers = getattr(mcp_config, "servers", None) if mcp_config else None
    for server in servers or []:
        if getattr(server, "tool_context_retention", None):
            retention.update(server.tool_context_retention)
        if getattr(server, "tool_context_projection", None):
            projection.update(server.tool_context_projection)
        recent_keep = max(
            recent_keep, getattr(server, "bypass_compaction_for_turns", 0) or 0
        )
    return retention, projection, recent_keep


def _parse_async_tool(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a ``role:"developer"`` message's content into its async_tool envelope.

    Returns ``None`` when the content isn't an ``async_tool`` envelope (for
    example a plain developer note), in which case the message is left alone.
    """
    content = msg.get("content")
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "async_tool":
        return None
    return parsed


def _unwrap_result(result: Any) -> Any:
    """Unwrap the Clairvoyance tool-result envelope into the payload.

    MCP HTTP handlers return ``{"status": "success", "data": "<json string>"}``
    (``mcp/__init__.py``), which is stored verbatim as the ``async_tool``
    envelope's ``result``. The chat compactor projects against the *payload*
    (``products[*].title`` etc.), so voice must strip the envelope before
    projecting; otherwise every collection degrades to a stub. When ``data`` is
    itself JSON-encoded, it is decoded so the keep-paths resolve against real
    keys. The chat path achieves the same unwrapping via ``result_normalizer``.
    Non-string and non-envelope values pass through unchanged.
    """
    if not isinstance(result, str):
        return result
    try:
        obj = json.loads(result)
    except (TypeError, ValueError):
        return result
    if isinstance(obj, dict) and "status" in obj and "data" in obj:
        data = obj["data"]
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False)
    return result


def compact_voice_tool_results(
    messages: List[Any],
    retention: Optional[Dict[str, str]] = None,
    recent_keep: int = 0,
    projection: Optional[Dict[str, List[str]]] = None,
) -> List[Any]:
    """Compact stale ``role:"developer"`` async_tool results in a voice context.

    Mirrors ``compact_tool_results_universal`` but targets the ``async_tool``
    envelope shape pipecat's voice assistant aggregator stores for MCP tools.
    Returns a new list; the input is never mutated.
    """
    if not messages:
        return list(messages)
    retention = retention or {}

    call_index: Dict[str, Dict[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            fn = call.get("function")
            if isinstance(call_id, str) and isinstance(fn, dict):
                call_index[call_id] = {
                    "name": fn.get("name", "?"),
                    "arguments": fn.get("arguments"),
                }

    dev_positions: List[int] = [
        i
        for i, msg in enumerate(messages)
        if isinstance(msg, dict)
        and msg.get("role") == "developer"
        and _parse_async_tool(msg) is not None
    ]
    keep_set = set(dev_positions[-recent_keep:]) if recent_keep > 0 else set()

    n_compacted = 0
    out: List[Any] = []
    for i, msg in enumerate(messages):
        if not (isinstance(msg, dict) and msg.get("role") == "developer"):
            out.append(msg)
            continue
        envelope = _parse_async_tool(msg)
        if envelope is None or i in keep_set:
            out.append(msg)
            continue
        call_meta = call_index.get(envelope.get("tool_call_id") or "")
        tool_name = call_meta["name"] if call_meta else None
        if (
            not tool_name
            or retention.get(tool_name, _DEFAULT_RETENTION) != "last_turn_only"
        ):
            out.append(msg)
            continue
        keep_paths = (projection or {}).get(tool_name)
        new_result: Optional[str] = None
        if keep_paths:
            new_result = _project_content(
                _unwrap_result(envelope.get("result")), keep_paths
            )
        if new_result is None:
            raw_args = call_meta.get("arguments") if call_meta else None
            try:
                parsed_args = (
                    json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                )
            except (TypeError, ValueError):
                parsed_args = raw_args
            new_result = _stub_for(tool_name, parsed_args)
        new_envelope = {**envelope, "result": new_result}
        out.append({**msg, "content": json.dumps(new_envelope, ensure_ascii=False)})
        n_compacted += 1

    if n_compacted:
        logger.debug(
            f"[context_compactor] (voice) rewrote {n_compacted} stale "
            "async_tool result message(s) to stubs/projections"
        )
    return out


class CompactedAssistantAggregator(LLMAssistantAggregator):
    """Assistant aggregator that compacts stale MCP tool results before the
    LLM runs.

    Overrides ``_handle_function_call_finished`` to compact the shared context
    right after a tool result is stored and *before* pipecat pushes the next
    LLM inference (``push_context_frame``). This is deterministic because the
    push happens later, in ``_handle_function_call_result``.

    Compaction is a no-op when the template declared no ``tool_context_retention``
    (empty map), keeping collections for non-MCP templates untouched.
    """

    def __init__(
        self,
        context: Any,
        *,
        retention: Optional[Dict[str, str]] = None,
        projection: Optional[Dict[str, List[str]]] = None,
        recent_keep: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(context, **kwargs)
        self._compact_retention = retention or {}
        self._compact_projection = projection or {}
        self._compact_recent_keep = recent_keep

    async def _handle_function_call_finished(
        self, frame: Any, in_progress_frame: Any
    ) -> None:
        await super()._handle_function_call_finished(frame, in_progress_frame)
        if not self._compact_retention:
            return
        self._context.transform_messages(
            lambda msgs: compact_voice_tool_results(
                msgs,
                retention=self._compact_retention,
                recent_keep=self._compact_recent_keep,
                projection=self._compact_projection,
            )
        )


class CompactedContextAggregatorPair(LLMContextAggregatorPair):
    """Context-aggregator pair whose assistant aggregator compacts MCP results.

    The pair must hand back the *same* compacting assistant instance to the
    pipeline and to event observers (``LLMContextAggregatorPair.assistant()``
    is also what ``ObserversManager`` subscribes to for
    ``on_assistant_turn_started/stopped``). Using this pair as a drop-in
    replacement for ``LLMContextAggregatorPair`` keeps a single shared
    instance, so assistant-turn observer events keep firing alongside
    compaction.
    """

    def __init__(
        self,
        context: Any,
        *,
        retention: Optional[Dict[str, str]] = None,
        projection: Optional[Dict[str, List[str]]] = None,
        recent_keep: int = 0,
        user_params: Optional[LLMUserAggregatorParams] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(context, user_params=user_params)
        self._compacted_assistant = CompactedAssistantAggregator(
            context,
            retention=retention,
            projection=projection,
            recent_keep=recent_keep,
        )

    def assistant(self) -> CompactedAssistantAggregator:
        return self._compacted_assistant
