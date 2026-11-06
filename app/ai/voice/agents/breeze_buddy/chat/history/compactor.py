"""Conversation-context compactor for chat turns.

Two entry points over the same policy engine:

* :func:`compact_tool_results` — walks the ANTHROPIC ``messages`` array
  right before it's sent to the LLM (the Claude driver path).
* :func:`compact_tool_results_universal` — walks the UNIVERSAL
  ``LLMContext`` message list (``role: "tool"`` entries) BEFORE provider
  adaptation; the Gemini driver path uses this, and any future provider
  can too.

Both rewrite stale tool-result payloads down to a 1-line stub (or an
identity projection). Goal: keep input-token cost bounded as a session
accumulates tool calls.

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

__all__ = ["compact_tool_results", "compact_tool_results_universal"]

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


# ---------------------------------------------------------------------------
# Identity projection — keep a slim, durable view instead of a bare stub
# ---------------------------------------------------------------------------
#
# A full stub drops *everything* a tool returned, which is fine for
# easy-to-re-derive blobs but catastrophic when the result carried stable
# identity the shopper will ask about later (a product's URL/handle, price,
# or the variant id needed to re-add it to cart). Re-deriving those costs a
# whole tool round-trip — and when the data isn't in context the model tends
# to refuse or *guess* (e.g. fabricate a product URL).
#
# Projection is the middle path: for a ``last_turn_only`` tool that declares a
# keep-list, the stale result is rewritten to ONLY the whitelisted paths
# (identity) and everything heavy (descriptions, media, full variant blobs) is
# dropped. Typically ~1% of the original tokens while preserving 100% of what
# follow-up turns need. The whitelist lives in the template (engine stays
# domain-blind); the grammar mirrors response_transform paths: ``a.b`` descends
# keys, ``a[*].b`` iterates a list at ``a`` and descends ``b`` on each item.

_PROJECTION_MARKER = "_pruned"


def _parse_keep_path(path: str) -> List[tuple]:
    """Parse a dotted keep-path into ``(key, is_array)`` segments.

    ``products[*].variants[*].id`` → ``[("products",True),("variants",True),
    ("id",False)]``. Returns ``[]`` for an empty path (caller skips it).
    """
    if not path:
        return []
    segments: List[tuple] = []
    for part in path.split("."):
        if part.endswith("[*]"):
            segments.append((part[:-3], True))
        else:
            segments.append((part, False))
    return segments


def _copy_path(src: Any, out: Dict[str, Any], segments: List[tuple]) -> None:
    """Copy the value(s) at ``segments`` from ``src`` into ``out``, building the
    nested dict/list structure on demand.

    Array segments are index-aligned across calls, so ``products[*].id`` and
    ``products[*].url`` land their fields on the *same* projected product
    objects. Silently no-ops on any shape mismatch (missing key, non-list under
    ``[*]``, non-dict mid-path) so a malformed result can never raise here.
    """
    if not segments or not isinstance(src, dict):
        return
    key, is_array = segments[0]
    rest = segments[1:]
    if key not in src:
        return
    val = src[key]

    if is_array:
        if not isinstance(val, list):
            return
        existing = out.get(key)
        if not isinstance(existing, list):
            existing = []
            out[key] = existing
        while len(existing) < len(val):
            existing.append({})
        if rest:
            for i, item in enumerate(val):
                _copy_path(item, existing[i], rest)
        # A terminal ``[*]`` (no rest) would copy whole list items, defeating
        # the projection — keep-lists always end on a scalar field, so ignore.
        return

    if rest:
        child = out.get(key)
        if not isinstance(child, dict):
            child = {}
            out[key] = child
        _copy_path(val, child, rest)
    else:
        # Terminal scalar/subtree: copy the value as-is. Safe to share the
        # reference — the projected message is only ever serialised, never
        # mutated, downstream.
        out[key] = val


def _project_keep(src: Dict[str, Any], paths: List[str]) -> Dict[str, Any]:
    """Build a new dict containing only the whitelisted ``paths`` of ``src``."""
    out: Dict[str, Any] = {}
    for p in paths:
        segs = _parse_keep_path(p)
        if segs:
            _copy_path(src, out, segs)
    return out


def _project_content(content: Any, paths: List[str]) -> Optional[str]:
    """Project a tool_result ``content`` string down to the keep-``paths``.

    Returns the compact JSON string of the projection, or ``None`` when the
    content isn't a JSON object we can project, or the projection came back
    empty (e.g. the result shape changed) — in which case the caller falls
    back to the 1-line stub so the block is still bounded.
    """
    if not isinstance(content, str):
        return None
    try:
        obj = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    projected = _project_keep(obj, paths)
    if not projected:
        return None
    projected[_PROJECTION_MARKER] = (
        "identity-only view (heavy fields pruned to save context) — "
        "re-call this tool if you need full detail"
    )
    try:
        return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _is_tool_result_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_result"


def _is_tool_use_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "tool_use"


def compact_tool_results(
    messages: List[Dict[str, Any]],
    retention: Optional[Dict[str, str]] = None,
    recent_keep: int = 1,
    projection: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """Return a copy of ``messages`` with stale tool_results compacted.

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
        projection: Optional map of tool name → list of keep-paths. When a
            ``last_turn_only`` tool has a keep-list here, its stale results are
            rewritten to an *identity projection* (only the whitelisted paths)
            instead of a bare stub — so the LLM keeps durable referents
            (product url/handle/price, variant ids) at ~1% of the tokens and
            never has to refuse or guess on follow-up turns. Tools without a
            keep-list fall back to the 1-line stub (prior behavior).

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
            # Compact this one. Prefer an identity projection when the tool
            # declares a keep-list (preserves url/handle/price/variant ids for
            # follow-up turns); otherwise fall back to the 1-line stub.
            tool_args = tool_meta.get("input") if tool_meta else None
            keep_paths = (projection or {}).get(tool_name)
            new_text: Optional[str] = None
            if keep_paths:
                new_text = _project_content(block.get("content"), keep_paths)
            if new_text is None:
                new_text = _stub_for(tool_name, tool_args)
            new_content[j] = {
                **block,
                "content": new_text,
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
            f"block(s) to stubs/projections (retention map: "
            f"{ {k: v for k, v in retention.items() if v != _DEFAULT_RETENTION} })"
        )
    return out


# ---------------------------------------------------------------------------
# Universal-shape variant (Gemini path — pre-adaptation)
# ---------------------------------------------------------------------------
#
# The universal LLMContext list carries tool exchanges as
#   assistant: {"role":"assistant","tool_calls":[{"id","function":{"name",
#               "arguments": <json-str>}}]}
#   tool:      {"role":"tool","tool_call_id","content": <json-str>}
# Compacting HERE (before the provider adapter converts to Gemini
# ``Content`` parts) keeps the engine provider-blind: signatures, text and
# function_call parts are untouched — only stale tool-RESULT content is
# rewritten, exactly like the Anthropic variant. Non-dict entries
# (LLMSpecificMessage — Gemini thought signatures) pass through verbatim.


def compact_tool_results_universal(
    messages: List[Any],
    retention: Optional[Dict[str, str]] = None,
    recent_keep: int = 1,
    projection: Optional[Dict[str, List[str]]] = None,
) -> List[Any]:
    """Universal-shape sibling of :func:`compact_tool_results`.

    Same policy semantics (``last_turn_only`` tools, ``recent_keep``
    newest results always intact, projection keep-lists preferred over
    stubs). Returns a new list; the input is never mutated.
    """
    if not messages:
        return list(messages)
    retention = retention or {}

    # tool_call_id → {name, args(JSON str)} off assistant tool_calls.
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

    tool_positions: List[int] = [
        i
        for i, msg in enumerate(messages)
        if isinstance(msg, dict) and msg.get("role") == "tool"
    ]
    keep_set = set(tool_positions[-recent_keep:]) if recent_keep > 0 else set()

    n_compacted = 0
    out: List[Any] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool" or i in keep_set:
            out.append(msg)
            continue
        call_meta = call_index.get(msg.get("tool_call_id") or "")
        tool_name = call_meta["name"] if call_meta else None
        if not tool_name or retention.get(tool_name, _DEFAULT_RETENTION) != (
            "last_turn_only"
        ):
            out.append(msg)
            continue
        keep_paths = (projection or {}).get(tool_name)
        new_text: Optional[str] = None
        if keep_paths:
            new_text = _project_content(msg.get("content"), keep_paths)
        if new_text is None:
            raw_args = call_meta.get("arguments") if call_meta else None
            try:
                parsed_args = (
                    json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                )
            except (TypeError, ValueError):
                parsed_args = raw_args
            new_text = _stub_for(tool_name, parsed_args)
        out.append({**msg, "content": new_text})
        n_compacted += 1

    if n_compacted:
        logger.debug(
            f"[context_compactor] (universal) rewrote {n_compacted} stale "
            "tool result message(s) to stubs/projections"
        )
    return out
