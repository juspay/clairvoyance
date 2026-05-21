"""Conversation-context compactor for chat turns.

Walks the Anthropic ``messages`` array right before it's sent to the LLM and
rewrites stale ``tool_result`` payloads down to a 1-line stub. Goal: keep
input-token cost bounded as a session accumulates tool calls.

Behavioral contract
-------------------
* The ``tool_use`` block (a small assistant-side dict carrying the tool name
  and args) is **always preserved** — that gives the LLM proof the tool ran
  and lets it spot when it should re-call.
* Only the matching ``tool_result.content`` is rewritten — to a deterministic
  stub like ``[pruned: search_catalog({"query":"red"}) ran earlier; re-call
  to refresh]``.
* Rewriting only happens for tool names whose ``tool_context_retention``
  policy is ``last_turn_only`` AND only for tool_result blocks older than
  the most recent ``recent_keep`` tool_result(s). The newest one (or two)
  always pass through untouched so the agent can reason about what the
  current turn just fetched.
* Tools not listed in the retention map are treated as ``session`` —
  preserved as-is (current behavior, no surprise drift).

Cache implications
------------------
Compaction mutates earlier messages, which **breaks Anthropic prompt-cache
hits** on the prefix that includes those messages. The caller (chat agent)
has made a deliberate trade: bounded context > marginal cache savings.
Don't add cache_control markers around compacted blocks; let the adapter
re-cache from the next stable boundary.

Why per-tool, not per-result-size
----------------------------------
A size-based heuristic ("compact any tool_result over 5K chars") would be
simpler but rigid. Per-tool policy lets the template author keep state-
bearing tools (cart) fully readable while pruning the heavy, easy-to-re-
derive ones (catalog search). The trade-off is one extra field on the
``McpServerConfig`` — small price for clean semantics.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.logger import logger

__all__ = ["compact_tool_results"]

# Tools whose retention is not explicitly set default to this. ``session``
# means "keep tool_result content as-is" — preserves backward compatibility
# for any merchant template that doesn't opt in.
_DEFAULT_RETENTION = "session"


def _stub_for(tool_name: str, tool_args: Any) -> str:
    """Format the replacement content for a compacted tool_result.

    Includes the tool name and a compact JSON form of the args so the LLM
    can see *what was previously asked* — enough to decide whether to
    re-call. Capped because tool args can themselves be large.
    """
    try:
        args_repr = json.dumps(tool_args, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        args_repr = str(tool_args)
    # Bound the args repr so a single bloated arg can't reinflate context.
    if len(args_repr) > 200:
        args_repr = args_repr[:197] + "..."
    return (
        f"[pruned: {tool_name}({args_repr}) ran earlier — "
        "re-call this tool if you need fresh data]"
    )


def _is_tool_result_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_result"


def _is_tool_use_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_use"


def compact_tool_results(
    messages: List[Dict[str, Any]],
    retention: Optional[Dict[str, str]] = None,
    recent_keep: int = 1,
) -> List[Dict[str, Any]]:
    """Return a copy of ``messages`` with stale tool_results rewritten to stubs.

    Args:
        messages: Anthropic-format conversation list. Each message has a
            ``role`` and ``content`` (which may be a string or a list of
            content blocks).
        retention: Map of tool name → ``"last_turn_only"`` | ``"session"``.
            Tools not in the map default to ``"session"`` (preserved).
            ``None`` or empty map → returns messages essentially unchanged
            (only does a shallow copy).
        recent_keep: How many most-recent tool_result blocks to leave
            untouched. Default 1: the assistant always sees the very last
            tool result intact (the one its current turn is reasoning
            about). Increase to 2 for "I might re-reference the last two
            calls" workflows.

    Returns:
        A new list of messages (input is not mutated). Tool_use blocks are
        always preserved; only stale tool_result content is rewritten.
    """
    if not messages:
        return list(messages)

    retention = retention or {}

    # Build a tool_use_id → (tool_name, tool_args) index by scanning assistant
    # messages. The pairing is by ``tool_use_id`` (Anthropic's contract) — the
    # tool_result block carries ``tool_use_id`` referring back to the
    # tool_use it answers.
    tool_use_index: Dict[str, Dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _is_tool_use_block(block):
                tu_id = block.get("id")
                if tu_id:
                    tool_use_index[tu_id] = {
                        "name": block.get("name", "?"),
                        "input": block.get("input"),
                    }

    # First pass: locate every tool_result block, in order, and decide which
    # ones to compact. We compact a tool_result iff:
    #   - the tool it answers has retention == "last_turn_only", AND
    #   - it is NOT in the last ``recent_keep`` tool_results.
    tool_result_positions: List[tuple] = []  # (msg_index, block_index, tool_name)
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if not _is_tool_result_block(block):
                continue
            tu_id = block.get("tool_use_id")
            tool_meta = tool_use_index.get(tu_id) if tu_id else None
            tool_name = tool_meta["name"] if tool_meta else "?"
            tool_result_positions.append((i, j, tool_name, tool_meta))

    # Decide which positions get compacted. The last ``recent_keep`` are
    # always preserved. Everything earlier with policy == last_turn_only
    # gets compacted.
    keep_set = (
        set((i, j) for i, j, _, _ in tool_result_positions[-recent_keep:])
        if recent_keep > 0
        else set()
    )

    # Second pass: build the output. Shallow-copy each message; deep-copy
    # only when we actually rewrite content (avoids unnecessary work).
    n_compacted = 0
    out: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            out.append(msg)
            continue

        new_content = list(msg["content"])  # shallow copy of the content list
        rewrote = False
        for j, block in enumerate(new_content):
            if not _is_tool_result_block(block):
                continue
            if (i, j) in keep_set:
                continue
            # Find the tool name; default to session if unknown.
            tu_id = block.get("tool_use_id")
            tool_meta = tool_use_index.get(tu_id) if tu_id else None
            tool_name = tool_meta["name"] if tool_meta else None
            if not tool_name:
                continue
            policy = retention.get(tool_name, _DEFAULT_RETENTION)
            if policy != "last_turn_only":
                continue
            # Compact this one.
            tool_args = tool_meta.get("input") if tool_meta else None
            new_content[j] = {
                **block,
                "content": _stub_for(tool_name, tool_args),
            }
            rewrote = True
            n_compacted += 1

        if rewrote:
            out.append({**msg, "content": new_content})
        else:
            out.append(msg)

    if n_compacted:
        logger.debug(
            f"[context_compactor] rewrote {n_compacted} stale tool_result "
            f"block(s) to stubs (retention map: "
            f"{ {k: v for k, v in retention.items() if v != _DEFAULT_RETENTION} })"
        )
    return out
