"""Anthropic-shape ↔ OpenAI/LLMContext-shape conversion for chat history.

Persistence format (``chat_message.content_blocks`` JSONB) is canonical
Anthropic content blocks. Pipecat's ``LLMContext`` speaks OpenAI shape
internally and converts on the wire to whichever provider is in use.
This module is the seam between those two shapes.

Why Anthropic-shape on disk:
- Pairs cleanly with the existing ``chat_message`` ``role`` CHECK
  constraint (``user``/``assistant`` only — tool_result blocks live
  inside a user row).
- Matches every reference implementation in the research set
  (`shop-chat-agent`, `claude-cookbooks`, `mcp-python-sdk`).
- Provider-stable: when we eventually swap Claude for a non-Anthropic
  model, the on-disk shape is still the standard.

Block shapes
------------
Assistant row blocks::

    [
        {"type": "text", "text": "..."},                         # optional
        {"type": "tool_use", "id": "...", "name": "...",
         "input": {...}},                                        # 0..N
    ]

User row blocks (text turn)::

    [{"type": "text", "text": "..."}]

User row blocks (tool-result turn — emitted between tool cycles)::

    [
        {"type": "tool_result", "tool_use_id": "...", "content": "..."},
        ...
    ]
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, cast

from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContextMessage

from app.core.logger import logger

# A content_block carrying this visibility marker is LLM-context only:
# the history loader concatenates it into the LLM's prior-turn memory,
# but every widget-facing read path strips it before responding. This
# is how internal-only memory (rendered-UI summaries today; tool-intent
# traces, refine_ui hints, eval breadcrumbs tomorrow) rides on the
# canonical content_blocks pipe without leaking onto the user's screen.
VISIBILITY_INTERNAL = "internal"

# ---------------------------------------------------------------------------
# Encoding: write side (turn-loop → DB)
# ---------------------------------------------------------------------------


def assistant_turn_to_blocks(
    text: str,
    tool_calls: List[FunctionCallFromLLM],
) -> List[Dict[str, Any]]:
    """Build an Anthropic-shape block list for one assistant turn-step.

    ``text`` may be empty (the LLM emitted only a tool_use); ``tool_calls``
    may be empty (the LLM emitted only prose). At least one of the two
    is always present when the caller invokes this — otherwise the turn
    step is a no-op and should not be persisted.
    """
    blocks: List[Dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.tool_call_id,
                "name": call.function_name,
                "input": dict(call.arguments),
            }
        )
    return blocks


def tool_results_to_user_blocks(
    pairs: List[tuple],
) -> List[Dict[str, Any]]:
    """Coalesce N tool results into one Anthropic-shape user turn.

    ``pairs`` is a list of ``(tool_call_id, result_payload)``. The
    result_payload is whatever our dispatcher returned (FlowResult-ish
    dict or string) — we JSON-encode for the on-disk content.
    """
    blocks: List[Dict[str, Any]] = []
    for tool_call_id, result_payload in pairs:
        if isinstance(result_payload, str):
            encoded = result_payload
        else:
            try:
                encoded = json.dumps(result_payload, default=str)
            except (TypeError, ValueError):
                encoded = str(result_payload)
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": encoded,
            }
        )
    return blocks


def plain_text_blocks(text: str) -> List[Dict[str, Any]]:
    """Single-element [text] block. Used for the user's prose question
    and for the final assistant prose reply."""
    return [{"type": "text", "text": text}]


def internal_text_block(text: str) -> Dict[str, Any]:
    """A text block flagged visibility=internal — LLM-context only.

    The history loader concatenates this into the LLM's prior-turn
    memory; widget-facing read paths strip it via [[filter_visible_blocks]].
    """
    return {"type": "text", "text": text, "visibility": VISIBILITY_INTERNAL}


# ---------------------------------------------------------------------------
# Visibility filtering: widget-facing read side
# ---------------------------------------------------------------------------


def filter_visible_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop any block tagged ``visibility=internal``.

    Pre-fix rows that carried the summary inline in a normal text block
    are left untouched — minimal-blast-radius fix that only governs new
    writes. Existing transcripts continue to display the legacy
    ``[ui rendered: …]`` tail until they age out.
    """
    if not blocks:
        return blocks
    return [b for b in blocks if b.get("visibility") != VISIBILITY_INTERNAL]


# ---------------------------------------------------------------------------
# Decoding: read side (DB → LLMContext seed)
# ---------------------------------------------------------------------------


def blocks_to_llm_context_messages(
    rows: List[Dict[str, Any]],
) -> List[LLMContextMessage]:
    """Convert a chronological list of chat_message rows into the
    OpenAI-shape LLMContextMessage list that ``LLMContext`` consumes.

    Each row is a dict with at least ``role`` and ``content_blocks``
    (or legacy ``content``). Tool_result blocks expand into multiple
    ``{role:"tool"}`` messages. Unknown block types are dropped with a
    warning so a forward-incompatible block never breaks replay.
    """
    out: List[LLMContextMessage] = []

    for row in rows:
        role = row.get("role")
        blocks = row.get("content_blocks")
        fallback_text = row.get("content")

        # Pre-migration rows or rows somehow missing content_blocks:
        # synthesise a text block from the denormalised content column.
        if not blocks and fallback_text:
            blocks = [{"type": "text", "text": fallback_text}]
        if not blocks:
            continue

        if role == "assistant":
            out.append(_assistant_row_to_openai(blocks))
        elif role == "user":
            out.extend(_user_row_to_openai(blocks))
        else:
            logger.warning(f"[block_codec] skipping unknown role {role!r}")

    return out


def _assistant_row_to_openai(blocks: List[Dict[str, Any]]) -> LLMContextMessage:
    """Anthropic assistant blocks → one OpenAI-shape assistant message.

    Concatenates text blocks; collects tool_use blocks into the
    OpenAI ``tool_calls`` array.
    """
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if t:
                text_parts.append(t)
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        else:
            logger.warning(
                f"[block_codec] skipping unknown assistant block type {btype!r}"
            )

    msg: Dict[str, Any] = {"role": "assistant"}
    if text_parts:
        msg["content"] = "".join(text_parts)
    else:
        msg["content"] = None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return cast(LLMContextMessage, msg)


def _user_row_to_openai(
    blocks: List[Dict[str, Any]],
) -> List[LLMContextMessage]:
    """Anthropic user blocks → 1+ OpenAI messages.

    Text-only blocks → one ``{role:"user"}`` message. Tool_result
    blocks → one ``{role:"tool"}`` message per result. A row can in
    theory mix both (Anthropic allows it); we emit the tool_result
    messages first (closer to the originating tool_use), then a
    user-text message if any.
    """
    text_parts: List[str] = []
    tool_msgs: List[LLMContextMessage] = []

    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if t:
                text_parts.append(t)
        elif btype == "tool_result":
            tool_msgs.append(
                cast(
                    LLMContextMessage,
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id"),
                        "content": block.get("content") or "",
                    },
                )
            )
        else:
            logger.warning(f"[block_codec] skipping unknown user block type {btype!r}")

    msgs: List[LLMContextMessage] = list(tool_msgs)
    if text_parts:
        msgs.append(
            cast(
                LLMContextMessage,
                {"role": "user", "content": "".join(text_parts)},
            )
        )
    return msgs


__all__ = [
    "VISIBILITY_INTERNAL",
    "assistant_turn_to_blocks",
    "tool_results_to_user_blocks",
    "plain_text_blocks",
    "internal_text_block",
    "filter_visible_blocks",
    "blocks_to_llm_context_messages",
]
